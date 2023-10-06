import csv
import logging
import random
import time
from itertools import product
from multiprocessing import Pool, cpu_count
from statistics import median

import pytorch_lightning as L
import torch
from sklearn.metrics import confusion_matrix
from torch import nn
from torch.utils.data import DataLoader

from common import calculate_precision_p_value, PREDICTION_THRESHOLD
from hyperparameter_tuning.load_data import load_data
from hyperparameter_tuning.nn_components import OPTIMIZER_CLASSES, WEIGHT_INITIALIZATIONS

# Hyperparameters Explored
LEARNING_RATE = 'Learning Rate'
MAX_EPOCHS = 'Max Epochs'
BATCH_SIZE = 'Batch Size'
HIDDEN_LAYERS = 'Hidden Layers'
LOSS_FUNCTION = 'Loss Function'
ACTIVATION_FUNCTION = 'Activation Function'
OPTIMIZER = 'Optimizer'
DROPOUT = 'Dropout'
L1_REGULARIZATION = 'L1 Regularization'
L2_REGULARIZATION = 'L2 Regularization'
WEIGHT_INITIALIZATION = 'Weight Initialization'

# Modify this!  Add all the possible values you want to explore.
# A word of caution: due to the multiplicative nature of iterations,
# each added value can significantly increase execution time.
hyperparameter_values = {
    LEARNING_RATE: [0.001, 0.0005],
    MAX_EPOCHS: [8],
    BATCH_SIZE: [32, 64],
    HIDDEN_LAYERS: [
        [2, 3, 2, 1, 0.5],
        [2, 3, 2, 1, 0.5, .25],
    ],
    # https://pytorch.org/docs/stable/nn.html#loss-functions
    LOSS_FUNCTION: [nn.MSELoss, nn.SmoothL1Loss, nn.HuberLoss],
    # https://pytorch.org/docs/stable/nn.html#non-linear-activations-weighted-sum-nonlinearity
    ACTIVATION_FUNCTION: [nn.LeakyReLU, nn.PReLU, nn.ReLU, nn.Tanh],
    OPTIMIZER: ['Adam', 'RMSprop'],
    DROPOUT: [0, 0.2, 0.5],
    L1_REGULARIZATION: [0],  # [0, 0.01, 0.1],
    L2_REGULARIZATION: [0],  # [0, 0.01, 0.1],
    WEIGHT_INITIALIZATION: ['xavier_uniform']
}

# Do you want to explore all combinations or a randomly selected subset?
# Searching all combinations is known as "grid search", a randomly selected subset is known as "random search"
EXPLORE_ALL_COMBINATIONS = True

# If we are doing a random search (you set `False` above)
# then how many combinations do you want to try?
NUMBER_OF_COMBINATIONS_TO_TRY = 100

# For stochastic methods such as training neural networks, results will vary.
# if one hyperparameter configuration outperforms another, how do we know
# this is not due to random variation?  One way to reduce this effect is to
# re-run the process multiple times and pick the median performance.
# This variable allows us to set how many times the process is re-run.
# The larger this number, the more random performance variation is reduced.
# However, the larger the number, the longer the execution time.
# Set this to 1 to run each configuration only once.
RERUN_COUNT = 5

# The number of CPUs to dedicate to this.  Minimum 1
CPU_COUNT = cpu_count()

# keeping output from being so verbose
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)

# loading data
train_dataset, X_test_scaled, Y_test, input_feature_size = load_data()

# Generate all the combinations of hyperparameter values
all_combinations = list(product(*hyperparameter_values.values()))

# Grid search vs. Random search
if EXPLORE_ALL_COMBINATIONS:
    combinations_to_try = all_combinations
else:
    # Get a random subset of combinations
    combinations_to_try = random.sample(all_combinations, min(NUMBER_OF_COMBINATIONS_TO_TRY, len(all_combinations)))


# Returns a NN class based on a set of hyperparameters
def neural_network(params):
    class SimpleNN(L.LightningModule):
        def __init__(self):
            super().__init__()

            # Calculate hidden layer sizes
            sizes = [int(round(input_feature_size * h)) for h in params[HIDDEN_LAYERS]]

            # Dynamically build layers
            all_sizes = [input_feature_size] + sizes + [1]
            self.layers = nn.ModuleList([nn.Linear(all_sizes[i], all_sizes[i + 1])
                                         for i in range(len(all_sizes) - 1)])

            # Settings
            self.activation_function = params[ACTIVATION_FUNCTION]()
            self.dropout = nn.Dropout(params[DROPOUT])
            self.init_weights()

        def init_weights(self):
            init_func = WEIGHT_INITIALIZATIONS.get(params[WEIGHT_INITIALIZATION])
            if init_func is None:
                raise ValueError(f"Weight initialization '{params[WEIGHT_INITIALIZATION]}' not recognized")

            for layer in self.layers:
                init_func(layer.weight)

        def forward(self, x):
            x = x.type(self.layers[0].weight.dtype)
            for i, layer in enumerate(self.layers):
                x = layer(x)
                if i < len(self.layers) - 1:  # Only apply dropout and activation to non-last layers
                    x = self.activation_function(x)
                    x = self.dropout(x)
            x = nn.Sigmoid()(x)  # Using Sigmoid for the output as I assume it's a binary classification
            return x

        def training_step(self, batch, batch_idx):
            x, y = batch
            y_hat = self(x)
            loss = params[LOSS_FUNCTION]()(y_hat, y.view(-1, 1).float())

            # L1 Regularization
            l1_reg = 0.0
            for param in self.parameters():
                l1_reg += torch.norm(param, 1)
            loss = loss + params[L1_REGULARIZATION] * l1_reg

            return loss

        def configure_optimizers(self):
            optimizer_class = OPTIMIZER_CLASSES.get(params[OPTIMIZER])
            if optimizer_class is None:
                raise ValueError(f"Optimizer '{params[OPTIMIZER]}' not recognized")

            optimizer = optimizer_class(self.parameters(), lr=params[LEARNING_RATE])

            # If optimizer has its own L2 regularization handling, skip. For instance, AdamW.
            if params[OPTIMIZER] not in ['AdamW'] and params[L2_REGULARIZATION] > 0:
                for group in optimizer.param_groups:
                    group['weight_decay'] = params[L2_REGULARIZATION]

            return optimizer

    return SimpleNN


def run_model(model_class, params):
    model = model_class()

    train_loader = DataLoader(train_dataset, batch_size=params[BATCH_SIZE])

    trainer = L.Trainer(max_epochs=params[MAX_EPOCHS], logger=False)
    trainer.fit(model, train_loader)

    model.eval()
    with torch.no_grad():
        predictions = model(torch.tensor(X_test_scaled)).numpy()

    predictions_bin = (predictions > PREDICTION_THRESHOLD).astype(int)

    # calculating p-value
    tn, fp, fn, tp = confusion_matrix(Y_test, predictions_bin).ravel()
    p_value = calculate_precision_p_value(tp=tp, fp=fp, fn=fn, tn=tn)

    return p_value


def evaluate_hyperparameters(params):
    model_class = neural_network(params)

    # running multiple times to get the median value
    # this helps account for random performance variation
    p_values = [run_model(model_class, params) for _ in range(RERUN_COUNT)]
    return median(p_values)


def evaluate_wrapper(args):
    iteration, values = args

    # converting tuples to dict
    params = {key: value for key, value in zip(hyperparameter_values.keys(), values)}

    try:
        start_time = time.time()
        p_value = evaluate_hyperparameters(params)
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"Iteration: {iteration} of {len(combinations_to_try)}")
        print(f"Parameters: {params}")
        print(f"P-value: {p_value}")
        return params, p_value, execution_time, None
    except Exception as err:
        print(f"ERROR WITH PRAMS: {params}")
        return params, None, None, str(err)


def iterate_hyperparameters():
    # Initialize lists to store the results and errors
    results = []
    errors = []

    # Setting pools across our CPUs
    pool = Pool(CPU_COUNT)

    # Enumerate the combinations to track the iteration number
    params_with_iter = list(enumerate(combinations_to_try, 1))

    # Using Pool to parallelize
    for params, p_value, execution_time, error in list(pool.map(evaluate_wrapper, params_with_iter)):

        if error is None:
            params.update({'p_value': p_value, 'execution_time': execution_time})
            results.append(params)
        else:
            params.update({'error': error})
            errors.append(params)

    # Finding the parameters that produced the lowest p-value
    best_result = min(results, key=lambda x: x['p_value'])
    print(f"The best parameters are: {best_result} with a p-value of {best_result['p_value']}")

    # Sorting the results by p-value in ascending order
    sorted_results = sorted(results, key=lambda x: x['p_value'])

    # Saving the sorted results to a CSV file
    with open('results/hyperparameter_results.csv', 'w', newline='') as csvfile:
        fieldnames = list(hyperparameter_values.keys()) + ['p_value', 'execution_time']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for result in sorted_results:
            writer.writerow(result)

    # If any errors occurred, record those
    if len(errors) > 0:
        with open('results/errors.csv', 'w', newline='') as errorFile:
            fieldnames = list(hyperparameter_values.keys()) + ['error']
            writer = csv.DictWriter(errorFile, fieldnames=fieldnames)
            writer.writeheader()
            for error in errors:
                writer.writerow(error)

    # Close the pool
    pool.close()
    pool.join()


# running the iterations
if __name__ == '__main__':
    iterate_hyperparameters()
