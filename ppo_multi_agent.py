import os
import time
import joblib
import numpy as np
import os.path as osp
import tensorflow as tf
from baselines import logger
from collections import deque
from baselines.common import explained_variance
import multiprocessing
import sys
from gym import spaces
import random
import gym
from config import Config
import utils

'''
Adds multi-agent support for PPO2
Heavily based on baselines.ppo2.ppo2

Runner must now determine actions for all agents.
Learning only happens from the perspective of the primary agent.
'''

class MultiModel(object):
    def __init__(self, main_model, opponent_model):
        self.opponent_model = opponent_model

        def multi_step(obs, opponent_obs, states, dones):
            actions, values, ret_states, neglogpacs = main_model.step(obs, states, dones)

            full_actions = []

            if self.opponent_model == None:
                for (i, a) in enumerate(actions):
                    full_actions.append([a, 1])
            else:
                opponent_actions, _, _, _ = self.opponent_model.step(opponent_obs, states, dones)

                for (i, a) in enumerate(actions):
                    full_actions.append([a, opponent_actions[i]])

            self.full_actions = full_actions

            return actions, values, ret_states, neglogpacs

        self.multi_step = multi_step
        self.value = main_model.value
            

class Model(object):
    def __init__(self, *, policy, ob_shape, ac_space, nbatch_act, nbatch_train,
                nsteps, ent_coef, vf_coef, max_grad_norm, scope_name):
        sess = tf.get_default_session()

        act_model = policy(sess, ob_shape, ac_space, nbatch_act, 1, scope_name, reuse=False)
        train_model = policy(sess, ob_shape, ac_space, nbatch_train, nsteps, scope_name, reuse=True)

        A = train_model.pdtype.sample_placeholder([None])
        ADV = tf.placeholder(tf.float32, [None])
        R = tf.placeholder(tf.float32, [None])
        OLDNEGLOGPAC = tf.placeholder(tf.float32, [None])
        OLDVPRED = tf.placeholder(tf.float32, [None])
        LR = tf.placeholder(tf.float32, [])
        CLIPRANGE = tf.placeholder(tf.float32, [])

        neglogpac = train_model.pd.neglogp(A)
        entropy = tf.reduce_mean(train_model.pd.entropy())

        vpred = train_model.vf
        vpredclipped = OLDVPRED + tf.clip_by_value(train_model.vf - OLDVPRED, - CLIPRANGE, CLIPRANGE)
        vf_losses1 = tf.square(vpred - R)
        vf_losses2 = tf.square(vpredclipped - R)
        vf_loss = .5 * tf.reduce_mean(tf.maximum(vf_losses1, vf_losses2))
        ratio = tf.exp(OLDNEGLOGPAC - neglogpac)
        pg_losses = -ADV * ratio
        pg_losses2 = -ADV * tf.clip_by_value(ratio, 1.0 - CLIPRANGE, 1.0 + CLIPRANGE)
        pg_loss = tf.reduce_mean(tf.maximum(pg_losses, pg_losses2))
        approxkl = .5 * tf.reduce_mean(tf.square(neglogpac - OLDNEGLOGPAC))
        clipfrac = tf.reduce_mean(tf.to_float(tf.greater(tf.abs(ratio - 1.0), CLIPRANGE)))
        loss = pg_loss - entropy * ent_coef + vf_loss * vf_coef

        with tf.variable_scope(scope_name):
            params = tf.trainable_variables(scope_name)
        grads = tf.gradients(loss, params)
        if max_grad_norm is not None:
            grads, _grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        grads = list(zip(grads, params))
        trainer = tf.train.AdamOptimizer(learning_rate=LR, epsilon=1e-5)
        _train = trainer.apply_gradients(grads)

        def train(lr, cliprange, obs, returns, masks, actions, values, neglogpacs, states=None):
            advs = returns - values
            advs = (advs - advs.mean()) / (advs.std() + 1e-8)

            td_map = {train_model.X:obs, A:actions, ADV:advs, R:returns, LR:lr,
                    CLIPRANGE:cliprange, OLDNEGLOGPAC:neglogpacs, OLDVPRED:values}
            if states is not None:
                td_map[train_model.S] = states
                td_map[train_model.M] = masks
            return sess.run(
                [pg_loss, vf_loss, entropy, approxkl, clipfrac, _train],
                td_map
            )[:-1]
        self.loss_names = ['policy_loss', 'value_loss', 'policy_entropy', 'approxkl', 'clipfrac']

        def save(save_file):
            ps = sess.run(params)
            joblib.dump(ps, Config.MODEL_DIR + save_file)

        update_placeholders = []
        update_ops = []

        for p in params:
            update_placeholder = tf.placeholder(p.dtype, shape=p.get_shape())
            update_placeholders.append(update_placeholder)
            update_op = p.assign(update_placeholder)
            update_ops.append(update_op)

        def load(load_file):
            loaded_params = joblib.load(Config.MODEL_DIR + load_file)

            feed_dict = {}

            for update_placeholder, loaded_p in zip(update_placeholders, loaded_params):
                feed_dict[update_placeholder] = loaded_p

            sess.run(update_ops, feed_dict=feed_dict)

        self.train = train
        self.train_model = train_model
        self.act_model = act_model
        self.step = act_model.step
        self.value = act_model.value
        self.initial_state = act_model.initial_state
        self.save = save
        self.load = load
        tf.global_variables_initializer().run(session=sess) #pylint: disable=E1101

class Runner(object):
    def __init__(self, *, env, model, opponent_model, nsteps, gamma, lam):
        self.env = env
        self.model = MultiModel(model, opponent_model)
        nenv = env.num_envs
        input_shape = utils.get_shape(env.observation_space)
        self.primary_obs = np.zeros((nenv,) + input_shape, dtype=model.train_model.X.dtype.name)
        self.opponent_obs = np.zeros((nenv,) + input_shape, dtype=model.train_model.X.dtype.name)
        multi_agent_obs = env.reset()
        self.use_multi_agent_obs(multi_agent_obs)
        self.gamma = gamma
        self.lam = lam
        self.nsteps = nsteps
        self.states = model.initial_state
        self.dones = [False for _ in range(nenv)]

    def use_multi_agent_obs(self, multi_agent_obs):
        self.primary_obs[:] = multi_agent_obs[:,:,:,0:3]
        self.opponent_obs[:] = multi_agent_obs[:,:,:,3:6]

    def run(self):
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones, mb_neglogpacs = [],[],[],[],[],[]
        mb_states = self.states
        epinfos = []
        for _ in range(self.nsteps):
            actions, values, self.states, neglogpacs = self.model.multi_step(self.primary_obs, self.opponent_obs, self.states, self.dones)
            mb_obs.append(self.primary_obs.copy())
            mb_actions.append(actions)
            mb_values.append(values)
            mb_neglogpacs.append(neglogpacs)
            mb_dones.append(self.dones)

            multi_agent_obs, rewards, self.dones, infos = self.env.step(self.model.full_actions)
            self.use_multi_agent_obs(multi_agent_obs)

            for info in infos:

                maybeepinfo = info.get('episode')
                if maybeepinfo: epinfos.append(maybeepinfo)

            mb_rewards.append(rewards)
        #batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=self.primary_obs.dtype)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
        mb_actions = np.asarray(mb_actions)
        mb_values = np.asarray(mb_values, dtype=np.float32)
        mb_neglogpacs = np.asarray(mb_neglogpacs, dtype=np.float32)
        mb_dones = np.asarray(mb_dones, dtype=np.bool)
        last_values = self.model.value(self.primary_obs, self.states, self.dones)
        #discount/bootstrap off value fn
        mb_returns = np.zeros_like(mb_rewards)
        mb_advs = np.zeros_like(mb_rewards)
        lastgaelam = 0
        for t in reversed(range(self.nsteps)):
            if t == self.nsteps - 1:
                nextnonterminal = 1.0 - self.dones
                nextvalues = last_values
            else:
                nextnonterminal = 1.0 - mb_dones[t+1]
                nextvalues = mb_values[t+1]
            delta = mb_rewards[t] + self.gamma * nextvalues * nextnonterminal - mb_values[t]
            mb_advs[t] = lastgaelam = delta + self.gamma * self.lam * nextnonterminal * lastgaelam
        mb_returns = mb_advs + mb_values
        return (*map(sf01, (mb_obs, mb_returns, mb_dones, mb_actions, mb_values, mb_neglogpacs)),
            mb_states, epinfos)

def sf01(arr):
    """
    swap and then flatten axes 0 and 1
    """
    s = arr.shape
    return arr.swapaxes(0, 1).reshape(s[0] * s[1], *s[2:])

def constfn(val):
    def f(_):
        return val
    return f

def learn(*, policy, env, nsteps, total_timesteps, ent_coef, lr,
            vf_coef=0.5,  max_grad_norm=0.5, gamma=0.99, lam=0.95,
            log_interval=10, nminibatches=4, noptepochs=4, cliprange=0.2,
            save_interval=0):

    if isinstance(lr, float): lr = constfn(lr)
    else: assert callable(lr)
    if isinstance(cliprange, float): cliprange = constfn(cliprange)
    else: assert callable(cliprange)
    total_timesteps = int(total_timesteps)

    nenvs = env.num_envs
    ob_shape = utils.get_shape(env.observation_space)
    ac_space = env.action_space
    nbatch = nenvs * nsteps
    nbatch_train = nbatch // nminibatches

    make_model = lambda scope_name: Model(policy=policy, ob_shape=ob_shape, ac_space=ac_space, nbatch_act=nenvs, nbatch_train=nbatch_train,
                    nsteps=nsteps, ent_coef=ent_coef, vf_coef=vf_coef, max_grad_norm=max_grad_norm, scope_name=scope_name)
    if save_interval and logger.get_dir():
        import cloudpickle
        with open(osp.join(logger.get_dir(), 'make_model.pkl'), 'wb') as fh:
            fh.write(cloudpickle.dumps(make_model))
    model = make_model(Config.PRIMARY_MODEL_SCOPE)
    opponent_model = None

    baseline_file = None
    # baseline_file = 'dual_snake_3.pkl'

    if Config.NUM_SNAKES > 1:
        opponent_model = make_model(Config.OPPONENT_MODEL_SCOPE)

    if baseline_file != None:
        model.load(baseline_file)

        if opponent_model != None:
            opponent_model.load(baseline_file)

    runner = Runner(env=env, model=model, opponent_model=opponent_model, nsteps=nsteps, gamma=gamma, lam=lam)

    maxlen = 100
    epinfobuf = deque(maxlen=maxlen)
    tfirststart = time.time()

    next_highscore = 5
    highscore_interval = 1

    opponent_save_interval = Config.OPPONENT_SAVE_INTERVAL
    max_saved_opponents = Config.MAX_SAVED_OPPONENTS
    opponent_idx = 0
    num_opponents = 0

    model.save(utils.get_opponent_file(opponent_idx))
    opponent_idx += 1
    num_opponents += 1

    nupdates = total_timesteps//nbatch
    for update in range(1, nupdates+1):
        if opponent_model != None:
            selected_opponent_idx = random.randint(0, max(num_opponents - 1, 0))
            print('Loading checkpoint ' + str(selected_opponent_idx) + '...')
            opponent_model.load(utils.get_opponent_file(selected_opponent_idx))

        assert nbatch % nminibatches == 0
        nbatch_train = nbatch // nminibatches
        tstart = time.time()
        frac = 1.0 - (update - 1.0) / nupdates
        lrnow = lr(frac)
        cliprangenow = cliprange(frac)
        obs, returns, masks, actions, values, neglogpacs, states, epinfos = runner.run() #pylint: disable=E0632
        epinfobuf.extend(epinfos)
        mblossvals = []
        
        inds = np.arange(nbatch)
        for _ in range(noptepochs):
            np.random.shuffle(inds)
            for start in range(0, nbatch, nbatch_train):
                end = start + nbatch_train
                mbinds = inds[start:end]
                slices = (arr[mbinds] for arr in (obs, returns, masks, actions, values, neglogpacs))
                mblossvals.append(model.train(lrnow, cliprangenow, *slices))

        lossvals = np.mean(mblossvals, axis=0)
        tnow = time.time()
        fps = int(nbatch / (tnow - tstart))

        ep_rew_mean = safemean([epinfo['r'] for epinfo in epinfobuf])

        if update % opponent_save_interval == 0 and opponent_model != None:
            print('Saving opponent model ' + str(opponent_idx) + '...')

            model.save(utils.get_opponent_file(opponent_idx))

            opponent_idx += 1
            num_opponents = max(opponent_idx, num_opponents)
            opponent_idx = opponent_idx % max_saved_opponents

        if update % log_interval == 0 or update == 1:
            if (Config.NUM_SNAKES == 1):
                logger.logkv('next_highscore', next_highscore)
            else:
                logger.logkv('num_opponents', num_opponents)
                
            ev = explained_variance(values, returns)
            logger.logkv("serial_timesteps", update*nsteps)
            logger.logkv("nupdates", update)
            logger.logkv("total_timesteps", update*nbatch)
            logger.logkv("fps", fps)
            logger.logkv("explained_variance", float(ev))
            logger.logkv('eprewmean ' + str(maxlen), ep_rew_mean)
            logger.logkv('eplenmean', safemean([epinfo['l'] for epinfo in epinfobuf]))
            logger.logkv('time_elapsed', tnow - tfirststart)
            logger.logkv('nenvs nsteps nmb nopte', [nenvs, nsteps, nminibatches, noptepochs])
            for (lossval, lossname) in zip(lossvals, model.loss_names):
                logger.logkv(lossname, lossval)
            logger.dumpkvs()
        if save_interval and (update % save_interval == 0 or update == 1) and logger.get_dir():
            checkdir = osp.join(logger.get_dir(), 'checkpoints')
            os.makedirs(checkdir, exist_ok=True)
            savepath = osp.join(checkdir, '%.5i'%update)
            print('Saving to', savepath)
            model.save(savepath)

        # Highscores only indicate better performance in single agent setting, free of opponent agent dependencies
        if (ep_rew_mean > next_highscore) and Config.NUM_SNAKES == 1:
            print('saving agent with new highscore ', next_highscore, '...')
            next_highscore += highscore_interval
            model.save('highscore_model.pkl')

    model.save('snake_model.pkl')
    
    env.close()

def safemean(xs):
    return np.nan if len(xs) == 0 else np.mean(xs)
