"PopArt-IMPALA for Atari"

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import contextlib
import functools
import os
import sys
import tensorflow as tf
import numpy as np

sys.path.insert(0,'..')
#from more_itertools import one 
import popart.vtrace_popart as vtrace
from utils import atari_utils
from utils import atari_environment
from utils import py_process
from utils.test import test
from popart.agent import agent_factory
from popart.agent import agent_factory
from popart.build_actor import build_actor
from popart.build_learner import build_learner
from popart.flags import *

try:
  import utils.dynamic_batching

except tf.errors.NotFoundError:
  tf.logging.warning('Running without dynamic batching.')

from six.moves import range
nest = tf.contrib.framework.nest

def is_single_machine():
    return FLAGS.task == -1


def create_atari_environment(env_id, seed, is_test=False):

  config = {
      'width': FLAGS.width,
      'height': FLAGS.height
  }

  if is_test:
    config['allowHoldOutLevels'] = 'true'
    # Mixer seed for evalution, see
    # https://github.com/deepmind/lab/blob/master/docs/users/python_api.md
    config['mixerSeed'] = 0x600D5EED

  process = py_process.PyProcess(atari_environment.PyProcessAtari, env_id, config, seed)
  proxy_env = atari_environment.FlowEnvironment(process.proxy)
  return proxy_env

@contextlib.contextmanager
def pin_global_variables(device):
  """Pins global variables to the specified device."""
  def getter(getter, *args, **kwargs):
    var_collections = kwargs.get('collections', None)
    if var_collections is None:
      var_collections = [tf.GraphKeys.GLOBAL_VARIABLES]
    if tf.GraphKeys.GLOBAL_VARIABLES in var_collections:
      with tf.device(device):
        return getter(*args, **kwargs)
    else:
      return getter(*args, **kwargs)

  with tf.variable_scope('', custom_getter=getter) as vs:
    yield vs

def train(action_set, level_names):
  """Train."""
  if is_single_machine():
    local_job_device = ''
    shared_job_device = ''
    is_actor_fn = lambda i: True
    is_learner = True
    global_variable_device = '/gpu'
    server = tf.train.Server.create_local_server()
    filters = []
  else:
    local_job_device = '/job:%s/task:%d' % (FLAGS.job_name, FLAGS.task)
    shared_job_device = '/job:learner/task:0'
    is_actor_fn = lambda i: FLAGS.job_name == 'actor' and i == FLAGS.task
    is_learner = FLAGS.job_name == 'learner'

    # Placing the variable on CPU, makes it cheaper to send it to all the
    # actors. Continual copying the variables from the GPU is slow.
    global_variable_device = shared_job_device + '/cpu'
    cluster = tf.train.ClusterSpec({
        'actor': ['localhost:%d' % (8001 + i) for i in range(FLAGS.num_actors)],
        'learner': ['localhost:8000']
    })
    server = tf.train.Server(cluster, job_name=FLAGS.job_name,
                             task_index=FLAGS.task)
    filters = [shared_job_device, local_job_device]

  # Only used to find the actor output structure.
  config = tf.ConfigProto(allow_soft_placement=True, device_filters=filters) 
  if is_learner:
    config.gpu_options.allow_growth = True
  
  Agent = agent_factory(FLAGS.agent_name)
  with tf.Graph().as_default():
    env = create_atari_environment(level_names[0], seed=1)
    agent = Agent(len(action_set))
    structure = build_actor(agent, env, level_names[0], action_set)
    flattened_structure = nest.flatten(structure)
    dtypes = [t.dtype for t in flattened_structure]    
    shapes = [t.shape.as_list() for t in flattened_structure]

  with tf.Graph().as_default(), \
       tf.device(local_job_device + '/cpu'), \
       pin_global_variables(global_variable_device):
    tf.set_random_seed(FLAGS.seed)  # Makes initialization deterministic.

    # Create Queue and Agent on the learner.
    with tf.device(shared_job_device):
      queue = tf.FIFOQueue(FLAGS.queue_capacity, dtypes, shapes, shared_name='buffer')
      agent = Agent(len(action_set))

      if is_single_machine() and 'dynamic_batching' in sys.modules:
        # For single machine training, we use dynamic batching for improved GPU
        # utilization. The semantics of single machine training are slightly
        # different from the distributed setting because within a single unroll
        # of an environment, the actions may be computed using different weights
        # if an update happens within the unroll.
        old_build = agent._build
        @dynamic_batching.batch_fn
        def build(*args):
          # print("experiment.py: args: ", args)
          with tf.device('/gpu'):
            return old_build(*args)
        tf.logging.info('Using dynamic batching.')
        agent._build = build

    # Build actors and ops to enqueue their output.
    enqueue_ops = []
    for i in range(FLAGS.num_actors):
      if is_actor_fn(i):
        level_name = level_names[i % len(level_names)]
        tf.logging.info('Creating actor %d with level %s', i, level_name)
        env = create_atari_environment(level_name, seed=i + 1)
        actor_output = build_actor(agent, env, level_name, action_set)
        with tf.device(shared_job_device):
          enqueue_ops.append(queue.enqueue(nest.flatten(actor_output)))

    # If running in a single machine setup, run actors with QueueRunners
    # (separate threads).
    if is_learner and enqueue_ops:

      tf.train.add_queue_runner(tf.train.QueueRunner(queue, enqueue_ops))

    # Build learner.
    if is_learner:
      # Create global step, which is the number of environment frames processed.
      tf.get_variable(
          'num_environment_frames',
          initializer=tf.zeros_initializer(),
          shape=[],
          dtype=tf.int64,
          trainable=False,
          collections=[tf.GraphKeys.GLOBAL_STEP, tf.GraphKeys.GLOBAL_VARIABLES])

      # Create batch (time major) and recreate structure.
      dequeued = queue.dequeue_many(FLAGS.batch_size)
      dequeued = nest.pack_sequence_as(structure, dequeued)

      def make_time_major(s):
        return nest.map_structure(
            lambda t: tf.transpose(t, [1, 0] + list(range(t.shape.ndims))[2:]), s)

      dequeued = dequeued._replace(
          env_outputs=make_time_major(dequeued.env_outputs),
          agent_outputs=make_time_major(dequeued.agent_outputs))

      with tf.device('/gpu'):
        # Using StagingArea allows us to prepare the next batch and send it to
        # the GPU while we're performing a training step. This adds up to 1 step
        # policy lag.
        flattened_output = nest.flatten(dequeued)
        area = tf.contrib.staging.StagingArea(
            [t.dtype for t in flattened_output],
            [t.shape for t in flattened_output])
        stage_op = area.put(flattened_output)

        # Returns an ActorOutput tuple -> (level name, agent_state, env_outputs, agent_output)
        data_from_actors = nest.pack_sequence_as(structure, area.get())

        # Unroll agent on sequence, create losses and update ops.
        output = build_learner(agent,
                               data_from_actors.env_outputs,
                               data_from_actors.agent_outputs,
                               data_from_actors.level_name_as_idx)
        
    # Create MonitoredSession (to run the graph, checkpoint and log).
    tf.logging.info('Creating MonitoredSession, is_chief %s', is_learner)
    # config.gpu_options.per_process_gpu_memory_fraction = 0.8
    
    with tf.train.MonitoredTrainingSession(
        server.target,
        is_chief=is_learner,
        checkpoint_dir=FLAGS.logdir,
        save_checkpoint_secs=600,
        save_summaries_secs=30,
        log_step_count_steps=50000,
        config=config,
        hooks=[py_process.PyProcessHook()]) as session:

      if is_learner:
        # Logging.
        level_returns = {level_name: [] for level_name in level_names}
        summary_dir = os.path.join(FLAGS.logdir, "logging")
        summary_writer = tf.summary.FileWriterCache.get(summary_dir)

        # Prepare data for first run.
        session.run_step_fn(
            lambda step_context: step_context.session.run(stage_op))

        # Execute learning and track performance.
        num_env_frames_v = 0

        # Comment to print out the parameter counts. 
        # print("total params:", np.sum([np.prod(v.get_shape().as_list()) for v in tf.trainable_variables()]))
        # params = tf.trainable_variables()
        # for elem in params:
        #   print(elem)
        # print("Params: ", [v.get_shape().as_list() for v in tf.trainable_variables()])
        
        while num_env_frames_v < FLAGS.total_environment_frames:
          level_names_v, done_v, infos_v, num_env_frames_v, mean, _, std, _ = session.run(
              (data_from_actors.level_name,) + output + (agent._std, ) + (stage_op,))

          level_names_v = np.repeat([level_names_v], done_v.shape[0], 0)

          for level_name, episode_return, episode_step, acc_episode_reward, acc_episode_step in zip(
              level_names_v[done_v],
              infos_v.episode_return[done_v],
              infos_v.episode_step[done_v],
              infos_v.acc_episode_reward[done_v],
              infos_v.acc_episode_step[done_v]):

            episode_frames = episode_step * FLAGS.num_action_repeats

            tf.logging.info('Level: %s Episode return: %f Acc return: %f after %d frames',
                            level_name, episode_return, acc_episode_reward, num_env_frames_v)

            summary = tf.summary.Summary()
            summary.value.add(tag=level_name + '/episode_return',
                              simple_value=episode_return)
            summary.value.add(tag=level_name + '/episode_frames',
                              simple_value=episode_frames)
            summary.value.add(tag=level_name + '/acc_episode_return',
                                simple_value=acc_episode_reward)
            summary.value.add(tag=level_name + '/acc_episode_frames',
                                simple_value=acc_episode_step)
            # summary.value.add(tag=level_name + '/game_mean', 
            #                   simple_value=mean[game_id[level_name]])
            # summary.value.add(tag=level_name + '/game_std',
            #                   simple_value=std[game_id[level_name]])
            summary_writer.add_summary(summary, num_env_frames_v)

            level_returns[level_name].append(episode_return)

          if min(map(len, level_returns.values())) >= 1 and FLAGS.multi_task == 1:
            no_cap = atari_utils.compute_human_normalized_score(level_returns,
                                                            per_level_cap=None)
            cap_100 = atari_utils.compute_human_normalized_score(level_returns,
                                                             per_level_cap=100)

            summary = tf.summary.Summary()
            summary.value.add(
                tag=(level_name + '/training_no_cap'), simple_value=no_cap)
            summary.value.add(
                tag=(level_name + '/training_cap_100'), simple_value=cap_100)

            summary_writer.add_summary(summary, num_env_frames_v)

            # Clear level scores.
            level_returns = {level_name: [] for level_name in level_names}


      else:
        # Execute actors (they just need to enqueue their output).
        while True:
          session.run(enqueue_ops)

def main(_):
  tf.logging.set_verbosity(tf.logging.INFO)
  action_set = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14 ,15 ,16, 17] 
  if FLAGS.multi_task == 1 and FLAGS.mode == 'train':
    level_names = atari_utils.ATARI_GAMES.keys()
  elif FLAGS.multi_task == 1 and FLAGS.mode == 'test':
    level_names = atari_utils.ATARI_GAMES.values()
  else:
    level_names = [FLAGS.level_name]
    action_set = atari_env.get_action_set(FLAGS.level_name)

  if FLAGS.mode == 'train':
    train(action_set, level_names)
  else:
    test(action_set, level_names)

if __name__ == '__main__':
    tf.app.run()    
