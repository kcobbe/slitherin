from gym.envs.registration import register

register(
    id='Snake-v0',
    entry_point='envs.snake:SnakeEnv',
    max_episode_steps=2000,
)