from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependencies
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
import math
from flags import *
from utils import *
from sklearn.decomposition import PCA

tfd = tf.contrib.distributions

# Tuning program settings
FLAGS = flags.FLAGS
FLAGS.learning_rate = 0.09 # change
FLAGS.num_hidden_layers = 7
FLAGS.num_neurons_per_layer = 3
FLAGS.activation_function = "sigmoid"
FLAGS.num_principal_components = 50
FLAGS.batch_size = 44 # kept constant under hyperopt
FLAGS.num_epochs = 10000 # kept constant under hyperopt

TRAIN_PERCENTAGE = 0.8

def build_input_pipeline(drug_data_path, batch_size,
                          number_of_principal_components):
  """Build an Iterator switching between train and heldout data.
  Args:
    `drug_data`: string representing the path to the .npy dataset.
    `batch_size`: integer specifying the batch_size for the dataset.
    `number_of_principal_components`: integer specifying how many principal components
    to reduce the dataset into.
  """
  # Build an iterator over training batches.
  with np.load(drug_data_path) as data:
    features = data["features"]
    labels = data["labels"]

    # PCA (sklearn)
    features = PCA(n_components=number_of_principal_components).fit_transform(features)

    # Splitting into training and validation sets
    train_range = int(TRAIN_PERCENTAGE * len(features))

    training_features = features[:train_range]
    training_labels = labels[:train_range]
    validation_features = features[train_range:]
    validation_labels = labels[train_range:]

    # Z-normalising: (note with respect to training data)
    training_features = (training_features - np.mean(training_features, axis=0))/np.std(training_features, axis=0)
    validation_features = (validation_features - np.mean(training_features, axis=0))/np.std(training_features, axis=0)

  # Create the tf.Dataset object
  training_dataset = tf.data.Dataset.from_tensor_slices((training_features, training_labels))

  # Shuffle the dataset (note shuffle argument much larger than training size)
  # and form batches of size `batch_size`
  training_batches = training_dataset.shuffle(20000).repeat().batch(batch_size)
  training_iterator = training_batches.make_one_shot_iterator()

  # Build a iterator over the heldout set with batch_size=heldout_size,
  # i.e., return the entire heldout set as a constant.
  heldout_dataset = tf.data.Dataset.from_tensor_slices(
      (validation_features, validation_labels))
  heldout_frozen = (heldout_dataset.take(len(validation_features)).
                    repeat().batch(len(validation_features)))
  heldout_iterator = heldout_frozen.make_one_shot_iterator()

  # Combine these into a feedable iterator that can switch between training
  # and validation inputs.
  # Here should the minibatch increment be defined 
  handle = tf.placeholder(tf.string, shape=[])
  feedable_iterator = tf.data.Iterator.from_string_handle(
      handle, training_batches.output_types, training_batches.output_shapes)
  features_final, labels_final = feedable_iterator.get_next()

  return features_final, labels_final, handle, training_iterator, heldout_iterator, train_range


def main(argv):
  # extract the activation function from the hyperopt spec as an attribute from the tf.nn module
  activation = getattr(tf.nn, FLAGS.activation_function)

  # define the graph
  with tf.Graph().as_default():
    (features, labels, handle,
     training_iterator, heldout_iterator, train_range) = build_input_pipeline(
         "drug_data.npz", FLAGS.batch_size, FLAGS.num_principal_components)

    # Building the Bayesian Neural Network. 
    # We are here using the Gaussian Reparametrization Trick
    # to compute the stochastic gradients as described in the paper
    with tf.name_scope("bayesian_neural_net", values=[features]):
      neural_net = tf.keras.Sequential()
      for i in range(FLAGS.num_hidden_layers):
        layer = tfp.layers.DenseReparameterization(
            units=FLAGS.num_neurons_per_layer,
            activation=activation,
            trainable=True,
            kernel_prior_fn=default_multivariate_normal_fn, # NormalDiag with hyperopt sigma
            kernel_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
            kernel_posterior_tensor_fn=lambda x: x.sample(),
            bias_prior_fn=default_multivariate_normal_fn, # NormalDiag with hyperopt sigma
            bias_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
            bias_posterior_tensor_fn=lambda x: x.sample()
            )
        neural_net.add(layer)
      neural_net.add(tfp.layers.DenseReparameterization(
        units=1, # one dimensional output
        activation=None, # since regression (outcome not bounded)
        trainable=True, # i.e subject to optimization
        kernel_prior_fn=default_multivariate_normal_fn, # NormalDiag
        kernel_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
        kernel_posterior_tensor_fn=lambda x: x.sample(),
        bias_prior_fn=default_multivariate_normal_fn, # NormalDiag
        bias_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
        bias_posterior_tensor_fn=lambda x: x.sample()
        ))
      predictions = neural_net(features)

      preds = []
      for _ in range(1000):
          preds.append(neural_net(features))

    MAP, var = tf.nn.moments(tf.squeeze(preds), axes=[0])
    
    # Compute the -ELBO as the loss, averaged over the batch size.
    neg_log_likelihood = tf.reduce_mean(tf.squared_difference(labels, predictions))
    kl = sum(neural_net.losses) / FLAGS.batch_size
    elbo_loss = kl + neg_log_likelihood

    # Build metrics for evaluation. Predictions are formed from a single forward
    # pass of the probabilistic layers. They are cheap but noisy predictions.
    accuracy, accuracy_update_op = tf.metrics.mean_squared_error(
        labels=labels, predictions=predictions)

    with tf.name_scope("train"):
      # define optimizer - we are using (stochastic) gradient descent
      opt = tf.train.GradientDescentOptimizer(learning_rate=FLAGS.learning_rate)

      # define that we want to minimize the loss (-ELBO)
      train_op = opt.minimize(elbo_loss)
      # start the session
      sess = tf.Session()
      # initialize the variables
      sess.run(tf.global_variables_initializer())
      sess.run(tf.local_variables_initializer())

      # Run the training loop
      train_handle = sess.run(training_iterator.string_handle())
      heldout_handle = sess.run(heldout_iterator.string_handle())
      
      # Run the epochs
      for epoch in range(FLAGS.num_epochs):
        _ = sess.run([train_op, accuracy_update_op],
                     feed_dict={handle: train_handle})
        
        if epoch % 100 == 0:
          loss_value, accuracy_value = sess.run(
            [elbo_loss, accuracy], feed_dict={handle: train_handle})
          loss_value_validation, accuracy_value_validation = sess.run(
            [elbo_loss, accuracy], feed_dict={handle: heldout_handle}
          )
          print("Epoch: {:>3d} Loss: [{:.3f}, {:.3f}] Accuracy: [{:.3f}, {:.3f}]".format(
              epoch, loss_value, loss_value_validation, accuracy_value, accuracy_value_validation))

        # Check if final epoch, if so return the validation loss for the last epoch             
        if epoch == FLAGS.num_epochs-1:
            final_loss, final_accuracy = sess.run(
                [elbo_loss, accuracy], feed_dict={handle: heldout_handle}
            )
            print("Final loss: [{:.3f}, {:.3f}] Final accuracy: [{:.3f}, {:.3f}]".format(
              loss_value, loss_value_validation, accuracy_value, accuracy_value_validation))

    with tf.name_scope("evaluate"):
        # interpolate the predictive distributions and get the percentiles to represent
        # an empirical credible interval for the predictions

        predictions = np.asarray([sess.run(predictions,
                                       feed_dict={handle: heldout_handle})
                              for _ in range(FLAGS.num_monte_carlo)])

        predictions = np.squeeze(predictions) # fix the dimensions into a flat matrix
        print(type(predictions))
        credible_intervals = [] # will be a matrix with with lower- and upper bound as columns
        # loop over the columns and compute the empirical credible interval
        modes = []
        for i in range(predictions.shape[1]):
            lb = np.percentile(predictions[:,i], 2.5)
            ub = np.percentile(predictions[:,i], 97.5)
            mode = np.mean(predictions[:,i])
            credible_intervals.append([lb, ub])
            modes.append(mode)

        # check how often the true vale is inside the credible interval
        with np.load("drug_data.npz") as data:
            labels = data["labels"]
            features = data["features"]
            train_range = int(TRAIN_PERCENTAGE * len(features))
            validation_labels = labels[train_range:]

        inside = 0
        SSE = 0
        for i in range(validation_labels.shape[0]):
            label = validation_labels[i]
            if label >= credible_intervals[i][0] and label <= credible_intervals[i][1]:
                inside += 1
            SSE += (label - modes[i])**2

        print("MSE", SSE/validation_labels.shape[0])
        print(inside/validation_labels.shape[0])


if __name__ == "__main__":
  tf.app.run()