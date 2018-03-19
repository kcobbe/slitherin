## Single agent and multi agent learning on Slitherin' in OpenAI gym

Train an agent to play Slitherin', either in a single-agent or multi-agent environment.  Currently only supports up to 2 snakes.

## Usage

Train single player snake:

    $ python train_snake.py

Train 2 snakes against each other:

    $ python train_snake.py --dual-snakes

Visualize single player agent:

    $ python enjoy_snake.py --file saved_model.pkl

Visualize 2 competing agents:

    $ python enjoy_snake.py --dual-snakes --file saved_model.pkl

The model in the specified file will be used to control both snakes.

## Saving Models

During single-agent training, the final model will be saved to saved_models/trained_model.pkl. In addition, when the agent (approximately) achieves a new highscore, that model will be saved to highscore_model.pkl. This is useful if training is terminated early.

During multi-agent training, opponent models are periodically saved to ./saved_models.  This directory can get large, depending on Config.MAX_SAVED_OPPONENTS.

## Results

In the single player setting, the agent reaches near optimal performance after ~5k updates (~60 mins on a GTX 1080 Ti and 28 CPUs).

In the multi agent setting, performance is harder to quantify.  Comparing scores against a random sample of past opponents, performance appears to converges after ~50k updates (~10 hrs on a GTX 1080 Ti and 28 CPUs), however this performance is suboptimal.  Agents develop some resonable attack/defense behaviors, but they also still frequently make avoidable mistakes.  Reducing these mistakes is a work in progress.

See ./videos for agent performance.

## Notes

This work has so far focused on a relatively small world size, as this makes the agents easier/faster to train.

