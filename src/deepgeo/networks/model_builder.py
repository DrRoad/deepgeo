import csv
import math
import numpy as np
import tensorflow as tf
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../'))
import common.filesystem as fs
import common.quality_metrics as qm
import common.visualization as vis
import dataset.utils as dsutils
import networks.fcn1s as fcn1s
import networks.fcn2s as fcn2s
import networks.fcn4s as fcn4s
import networks.fcn8s as fcn8s
import networks.fcn32s as fcn32s
import networks.unet as unet
import networks.laterfusion.unet_lf as unet_lf
import networks.loss_functions as lossf
import networks.tb_metrics as tbm
import networks.layers as layers


def _rot90(image, label):
    image = tf.image.rot90(image, 1)
    label = tf.image.rot90(label, 1)
    return image, label


def _rot180(image, label):
    image = tf.image.rot90(image, 2)
    label = tf.image.rot90(label, 2)
    return image, label


def _rot270(image, label):
    image = tf.image.rot90(image, 3)
    label = tf.image.rot90(label, 3)
    return image, label


def _flip_left_right(image, label):
    image = tf.image.flip_left_right(image)
    image = tf.image.flip_left_right(label)
    return image, label


def _flip_up_down(image, label):
    image = tf.image.flip_up_down(image)
    image = tf.image.flip_up_down(label)
    return image, label


def _flip_transpose(image, label):
    image = tf.image.transpose_image(image)
    image = tf.image.transpose_image(label)
    return image, label


def _parse_function(serialized):
    features = {'image': tf.FixedLenFeature([], tf.string, default_value=''),
                'channels': tf.FixedLenFeature([], tf.int64, default_value=0),
                'label': tf.FixedLenFeature([], tf.string, default_value=''),
                'height': tf.FixedLenFeature([], tf.int64, default_value=0),
                'width': tf.FixedLenFeature([], tf.int64, default_value=0)}

    parsed_features = tf.parse_single_example(serialized=serialized, features=features)
    num_bands = tf.cast(parsed_features['channels'], tf.int32)
    height = parsed_features['height']
    width = parsed_features['width']
    image = tf.decode_raw(parsed_features['image'], tf.float32)
    image = tf.reshape(image, [286, 286, 10])

    label = tf.decode_raw(parsed_features['label'], tf.int32)
    label = tf.reshape(label, [286, 286, 1])
    return image, label


def tfrecord_input_fn(train_dataset, params, train=True):
    dataset = tf.data.TFRecordDataset(train_dataset)
    train_input = dataset.map(_parse_function, num_parallel_calls=10)
    if train:
        train_input = train_input.map(_rot90, num_parallel_calls=10)
        train_input = train_input.map(_rot180, num_parallel_calls=10)
        train_input = train_input.map(_rot270, num_parallel_calls=10)
        #train_input = train_input.map(_flip_left_right, num_parallel_calls=10)
        #train_input = train_input.map(_flip_up_down, num_parallel_calls=10)
        #train_input = train_input.map(_flip_transpose, num_parallel_calls=10)
        train_input = train_input.shuffle(10000)
        #train_input = train_input.repeat(params['epochs'])
    #else:
        #train_input.repeat(1)
    train_input = train_input.repeat(1).batch(params['batch_size'])
    train_input = train_input.prefetch(1000)
    return train_input


# TODO: Remove this
def discretize_values(data, number_class, start_value=0):
    for clazz in range(start_value, (number_class + 1)):
        if clazz == start_value:
            class_filter = (data <= clazz + 0.5)
        elif clazz == number_class:
            class_filter = (data > clazz - 0.5)
        else:
            class_filter = np.logical_and(data > clazz - 0.5, data <= clazz + 0.5)
        data[class_filter] = clazz

    return data.astype(np.uint8)


# TODO: Implement in the ModelBuilder a function that computes the output size.
class ModelBuilder(object):
    default_params = {
        'epochs': None,
        'batch_size': 10,
        'learning_rate': 0.001,
        'learning_rate_decay': True,
        'decay_rate': 0.1,
        'decay_steps': 245,
        'l2_reg_rate': 0.5,
        'dropout_rate': 0.5,
        'var_scale_factor': 2.0,
        'chips_tensorboard': 2,
        'fusion': 'none',
        'loss_func': 'crossentropy',
        'bands_plot': [0, 1, 2]
    }

    predefModels = {
        "fcn1s": fcn1s.fcn1s_description,
        "fcn2s": fcn2s.fcn2s_description,
        "fcn4s": fcn4s.fcn4s_description,
        "fcn8s": fcn8s.fcn8s_description,
        "fcn32s": fcn32s.fcn32s_description,
        "unet": unet.unet_description,
        "unet_lf": unet_lf.unet_lf_description
    }

    losses_switcher = {
        'cross_entropy': tf.losses.softmax_cross_entropy,
        'weighted_crossentropy': lossf.weighted_cross_entropy,
        'soft_dice': lossf.avg_soft_dice
    }

    predefClassif = {
        'sigmoid': tf.nn.sigmoid,
        'softmax': tf.nn.softmax
    }

    def __init__(self, model):
        if isinstance(model, str):
            self.network = model
            self.model_description = self.predefModels[model]
        else:
            self.network = "custom"  # TODO: Change this. Implement a registration for new strategies.
            self.model_description = model

    def __build_model(self, features, labels, params, mode, config):
        tf.logging.set_verbosity(tf.logging.INFO)
        training = mode == tf.estimator.ModeKeys.TRAIN
        # global_step = tf.Variable(0, name='global_step', trainable=False)
        samples = features#['data']

        logits = self.model_description(samples, labels, params, mode, config)

        predictions = tf.nn.softmax(logits, name='Softmax')
        output = tf.expand_dims(tf.argmax(input=predictions, axis=-1, name='Argmax_Prediction'), -1)

        if mode == tf.estimator.ModeKeys.PREDICT:
            return tf.estimator.EstimatorSpec(mode=mode, predictions={'classes': output})

        if labels.shape[1] != logits.shape[1]:
            labels = tf.cast(layers.crop_features(labels, logits.shape[1], name="labels"), tf.float32)

        labels_1hot = tf.one_hot(tf.cast(labels, tf.uint8), params['num_classes'])
        labels_1hot = tf.squeeze(labels_1hot)

        # loss_params = {
        #     'logits': logits,
        #     'predictions': predictions,
        #     'output': output,
        #     'labels_1hot': labels_1hot,
        #     'labels': labels,
        #     'class_weights': params['class_weights'],
        #     'num_classes': params['num_classes']
        # }

        # loss = tf.losses.sigmoid_cross_entropy(labels_1hot, output)
        # loss = lossf.weighted_binary_cross_entropy(logits, labels, params['class_weights'])
        # loss = tf.losses.softmax_cross_entropy(labels_1hot, logits)
        # loss = lossf.twoclass_cost(output, labels)
        # loss = lossf.inverse_mean_iou(logits, labels_1hot, num_classes)
        # loss = lossf.avg_soft_dice(logits, labels_1hot)
        loss = lossf.weighted_cross_entropy(logits, labels_1hot, params['class_weights'], params['num_classes'],
                                            training)
        # loss_func = self.losses_switcher.get(params['loss_func'], lossf.unknown_loss_error)
        # loss = loss_func(loss_params)

        tbm.plot_chips_tensorboard(samples, labels, tf.expand_dims(predictions[:, :, :, 2], -1), params)
        metrics, summaries = tbm.define_quality_metrics(labels_1hot, predictions, logits, labels, output, loss, params)

        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

        if params['learning_rate_decay']:
            params['learning_rate'] = tf.train.exponential_decay(learning_rate=params['learning_rate'],
                                                                 global_step=tf.train.get_global_step(),
                                                                 decay_rate=params['decay_rate'],
                                                                 decay_steps=params['decay_steps'],
                                                                 name='decrease_lr')

        tf.summary.scalar('learning_rate', params['learning_rate'])

        optimizer = tf.train.AdamOptimizer(learning_rate=params['learning_rate'], name='Optimizer')
        # optimizer = tf.contrib.opt.NadamOptimizer(params['learning_rate'], name='Optimizer')

        if training:
            with tf.control_dependencies(update_ops):
                train_op = optimizer.minimize(loss=loss, global_step=tf.train.get_global_step())
        else:
            train_op = None

        train_summary_hook = tf.train.SummarySaverHook(save_steps=100,
                                                       output_dir=config.model_dir,
                                                       summary_op=tf.summary.merge_all())

        eval_metric_ops = {'eval_metrics/accuracy': metrics['accuracy'],
                           'eval_metrics/f1-score': metrics['f1_score'],
                           'eval_metrics/cross_entropy': metrics['cross_entropy'],
                           'eval_metrics/auc_roc': metrics['auc-roc']}  # ,
                           # 'eval_metrics/mean_iou': metrics['mean_iou']}

        logging_hook = tf.train.LoggingTensorHook({'loss': loss,
                                                   'accuracy': metrics['accuracy'][1],
                                                   'f1_score': metrics['f1_score'][1],
                                                   'cross_entropy': metrics['cross_entropy'][1],
                                                   # 'mean_iou': metrics['mean_iou'][0],
                                                   'learning_rate': params['learning_rate'],
                                                   'auc_roc': metrics['auc-roc'][1]},
                                                  every_n_iter=100)

        eval_summary_hook = tf.train.SummarySaverHook(save_steps=100,
                                                      output_dir=config.model_dir + "/eval",
                                                      summary_op=tf.summary.merge_all())

        return tf.estimator.EstimatorSpec(mode=mode,
                                          predictions=output,
                                          loss=loss,
                                          train_op=train_op,
                                          eval_metric_ops=eval_metric_ops,
                                          evaluation_hooks=[eval_summary_hook, logging_hook],
                                          training_hooks=[train_summary_hook, logging_hook])

    # def train(self, train_imgs, test_imgs, train_labels, test_labels, params, output_dir):
    def train(self, train_dataset, test_dataset, params, output_dir):
        # tf.set_random_seed(1987)
        tf.logging.set_verbosity(tf.logging.INFO)

        if not os.path.exists(output_dir):
            fs.mkdir(output_dir)
            
        with open(os.path.join(output_dir, "parameters.csv"), "w") as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(["network", self.network])
            # w.writerow(["input_chip_size", [train_imgs[0].shape[0], train_imgs[0].shape[1]]])
            # w.writerow(["num_channels", train_imgs[0].shape[2]])
            for key, value in params.items():
                w.writerow([key, value])

        # data_size, _, _, bands = train_imgs.shape
        params['bands'] = 10  # bands
        params['decay_steps'] = math.ceil(33061 / params['batch_size'])  # math.ceil(data_size / params['batch_size'])

        # https://www.tensorflow.org/guide/distribute_strategy
        strategy = tf.contrib.distribute.MirroredStrategy()
        config = tf.estimator.RunConfig(train_distribute=strategy)  # , eval_distribute=strategy)

        estimator = tf.estimator.Estimator(model_fn=self.__build_model,
                                           model_dir=output_dir,
                                           params=params,
                                           config=config)

        # profiling_hook = tf.train.ProfilerHook(save_steps=10, output_dir=path.join(output_dir))

        for epoch in range(1, params['epochs'] + 1):
            print('===============================================')
            print('Epoch ', epoch)
       
            print('---------------')
            print('Training...')
            train_results = estimator.train(input_fn=lambda: tfrecord_input_fn(train_dataset, params),
                                            steps=None)
                                            # hooks=[profiling_hook])
       
            print('---------------')
            print('Evaluating...')
            test_results = estimator.evaluate(input_fn=lambda: tfrecord_input_fn(test_dataset, params))

        # early_stopping = tf.contrib.estimator.stop_if_no_decrease_hook(
        #     estimator,
        #     metric_name='cost/loss',
        #     max_steps_without_decrease=1000,
        #     eval_dir=path.join(output_dir, "eval"),
        #     min_steps=100)

        #tf.estimator.train_and_evaluate(estimator,
        #                                train_spec=tf.estimator.TrainSpec(lambda: tfrecord_input_fn(train_dataset, params)),
        #                                eval_spec=tf.estimator.EvalSpec(lambda: tfrecord_input_fn(test_dataset, params)))

    def validate(self, images, expect_labels, params, model_dir, save_results=True, exclude_classes=None):
        tf.logging.set_verbosity(tf.logging.WARN)

        out_dir = os.path.join(model_dir, 'validation')

        estimator = tf.estimator.Estimator(#model_fn=tf.contrib.estimator.replicate_model_fn(self.__build_model),
                                           model_fn=self.__build_model,
                                           model_dir=model_dir,
                                           params=params)

        data_size, _, _, _ = images.shape
        input_fn = tf.estimator.inputs.numpy_input_fn(x={'data': images},
                                                      batch_size=params['batch_size'],
                                                      shuffle=False)

        predictions_lst = []
        crop_labels = []
        for predict, label in zip(estimator.predict(input_fn), expect_labels):
            predictions_lst.append(predict['classes'])
            size_x, size_y, _ = predict['classes'].shape
            label = dsutils.crop_np_chip(label, size_x)
            crop_labels.append(label)

        predictions = np.array(predictions_lst, dtype=np.int32).flatten()
        crop_labels = np.array(crop_labels, dtype=np.int32).flatten()

        out_str = ''
        out_str += '<<------------------------------------------------------------>>' + os.linesep
        out_str += '<<------------------ Validation Results ---------------------->>' + os.linesep
        out_str += '<<------------------------------------------------------------>>' + os.linesep

        metrics, report_str = qm.compute_quality_metrics(crop_labels, predictions, params)

        out_str += report_str

        fs.mkdir(os.path.join(model_dir, 'validation'))
        print(out_str)

        report_path = os.path.join(out_dir, 'validation_report.txt')
        out_file = open(report_path, 'w')
        out_file.write(out_str)
        out_file.close()

        conf_matrix_path = os.path.join(out_dir, 'validation_confusion_matrix.png')
        auc_roc_path = os.path.join(out_dir, 'auc_roc_curve.png')
        vis.plot_confusion_matrix(metrics['confusion_matrix'], params, conf_matrix_path)
        vis.vis.plot_roc_curve(metrics['roc_score'], auc_roc_path)

    def predict(self, chip_struct, params, model_dir):
        tf.logging.set_verbosity(tf.logging.WARN)
        images = chip_struct['chips']

        estimator = tf.estimator.Estimator(model_fn=tf.contrib.estimator.replicate_model_fn(self.__build_model),
                                           # model_fn=self.__build_model,
                                           model_dir=model_dir,
                                           params=params)

        data_size, _, _, _ = images.shape
        input_fn = tf.estimator.inputs.numpy_input_fn(x={'data': images},
                                                      batch_size=params['batch_size'],
                                                      shuffle=False)

        # predictions = estimator.predict(input_fn=input_fn)

        print('Classifying image with structure ', str(images.shape), '...')

        predictions = []

        for predict in estimator.predict(input_fn):
            # for predict, dummy in zip(predictions, images):
            # predicted_images.append(np.argmax(predict["probabilities"], -1))
            # classif = np.argmax(predict["probabilities"], axis=-1)
            # predicted_images.append(discretize_values(predict["classes"],
            #                                           params["num_classes"],
            #                                           0))
            predictions.append(predict['classes'])
        chip_struct['predict'] = np.array(predictions, dtype=np.int32)

        return chip_struct

