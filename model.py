"""
Denoising AutoEncoder
"""

import os
import glob
import copy
import random

import obspy
import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt

from datetime import datetime
from scipy.signal import stft, istft
from tensorflow.keras.utils import Sequence
from tensorflow.keras.models import Model as TFmodel
from tensorflow.keras.layers import Input, Conv2D, BatchNormalization, ReLU, Dropout, Conv2DTranspose, Cropping2D, \
    MaxPooling2D, UpSampling2D, Dense, Softmax, Flatten, Reshape, Add, LeakyReLU

from utils import save_obj


def preprocessing(data, dt=1.0, **kwargs):
    # Remove mean
    data = data - np.mean(data)

    # Write data to obspy trace
    trace = obspy.Trace(data=data, header=dict(delta=dt))

    # Decimate data
    try:
        if kwargs['decimation_factor']:
            trace.decimate(kwargs['decimation_factor'], no_filter=True)
            dt = trace.stats.delta
    except KeyError:
        pass

    # Apply filter
    try:
        trace.filter(**kwargs['filter'])
    except KeyError:
        pass

    # Taper data
    try:
        trace.taper(**kwargs['taper'])
    except KeyError:
        pass

    return trace.data, dt


def cropping_layer(needed_shape, is_shape):
    diff1 = is_shape[0] - needed_shape[0]
    if diff1 % 2 == 0 and diff1 > 0:
        shape1 = (diff1//2, diff1//2)
    elif diff1 % 2 == 1 and diff1 > 0:
        shape1 = (diff1//2, is_shape[0] - needed_shape[0])
    elif diff1 == 0:
        shape1 = (0, 0)

    diff2 = is_shape[1] - needed_shape[1]
    if diff2 % 2 == 0 and diff2 > 0:
        shape2 = (diff2//2, diff2//2)
    elif diff2 % 2 == 1 and diff2 > 0:
        shape2 = (diff1//2, is_shape[1] - needed_shape[1])
    elif diff2 == 0:
        shape2 = (0, 0)

    return shape1, shape2



class Model:

    def __init__(self, ts_length=6001, dt=1.0, optimizer="adam",
                 loss='mean_absolute_error', activation=None, drop_rate=0.1,
                 use_bias=False, data_augmentation=True, shuffle=True, channels=2, decimation_factor=2,
                 callbacks=None, **kwargs):

        # Check input parameters
        if isinstance(decimation_factor, int) is True or isinstance(decimation_factor, float) is True:
            decimation_factor = int(decimation_factor)
            if decimation_factor <= 1:
                decimation_factor = None
        elif isinstance(decimation_factor, int) is False and decimation_factor is not None:
            msg = "Decimation factor must either be None or of type integer!"
            raise TypeError(msg)

        self.dt = dt
        self.dt_orig = dt
        self.ts_length = ts_length
        self.optimizer = optimizer
        self.loss = loss
        self.activation = activation
        self.drop_rate = drop_rate
        self.use_bias = use_bias
        self.data_augmentation = data_augmentation
        self.shuffle = shuffle
        self.channels = channels
        self.decimation_factor = decimation_factor
        self.kwargs = kwargs
        self.history = None
        self.callbacks = callbacks
        self.depth = None
        self.kernel_size = None
        self.strides = None
        self.filter_root = None
        self.fully_connected = None
        self.max_pooling = None

        # Get date and time
        self.now = datetime.now()
        self.now_str = self.now.strftime("%d-%m-%Y_%H-%M-%S")

        dummy = obspy.Trace(data=np.random.rand(ts_length), header=dict(delta=self.dt))
        if decimation_factor:
            dummy.decimate(factor=decimation_factor)
            # Get new dt after decimation
            self.dt = dummy.stats.delta
        _, _, dummystft = stft(dummy.data, fs=1 / self.dt, **kwargs)
        self.shape = dummystft.shape
        # Test whether stft is invertible and results in same length as input length
        t, dummy_x = istft(Zxx=dummystft, fs=1 / self.dt, **kwargs)
        if len(dummy_x) != len(dummy.data):
            msg = "Keywordarguments of STFT and ISTFT do not fit. \nThus, length of inverse STFT is {} which is " \
                  "not equal with length {} if input data.\nThis might lead to an error, when applying the " \
                  "trained model.\nPlease change your keywordarguments.".format(len(dummy_x), len(dummy.data))
            raise ValueError(msg)


    def build_model(self, filter_root=8, depth=4, kernel_size=(3, 3), fully_connected=False, strides=(2, 2),
                    max_pooling=False, **kwargs):

        self.filter_root = filter_root
        self.depth = depth
        self.kernel_size = kernel_size
        self.fully_connected = fully_connected
        self.strides = strides
        self.max_pooling = max_pooling

        # Pooling vs stride for downsampling:
        # https://stats.stackexchange.com/questions/387482/pooling-vs-stride-for-downsampling
        if max_pooling is True:
            pool_size = copy.copy(strides)
            strides = (1, 1)

        # Run Model on mutiple GPUs
        # Unccomment ntex two lines and tab everything including model.compile
        # mirrored_strategy = tf.distribute.MirroredStrategy()
        # with mirrored_strategy.scope():

        # Define Input layer
        input_layer = Input((self.shape[0], self.shape[1], self.channels))

        # Empty dict to save shape for each layer
        layer_shapes = dict()
        convs = []

        # Encoder
        # First Layer
        h = Conv2D(filter_root, kernel_size, activation=self.activation, padding='same',
                   use_bias=self.use_bias, **kwargs)(input_layer)
        h = BatchNormalization()(h)
        # h = ReLU()(h)
        h = LeakyReLU(alpha=0.1)(h)
        if self.drop_rate > 0.:
            h = Dropout(rate=self.drop_rate)(h)

        # More Layers
        for i in range(depth):
            h = Conv2D(int(2 ** i * filter_root), kernel_size, activation=self.activation, padding='same',
                       use_bias=self.use_bias, **kwargs)(h)
            h = BatchNormalization()(h)
            #h = ReLU()(h)
            h = LeakyReLU(alpha=0.1)(h)
            if self.drop_rate > 0.:
                h = Dropout(rate=self.drop_rate)(h)

            layer_shapes.update({i: (h.shape[1], h.shape[2])})
            convs.append(h)

            if i < depth - 1:
                h = Conv2D(int(2 ** i * filter_root), kernel_size, activation=self.activation, padding='same',
                           use_bias=self.use_bias, strides=strides, **kwargs)(h)

                if max_pooling is True:
                    h = MaxPooling2D(pool_size=pool_size, padding="same")(h)

                h = BatchNormalization()(h)
                #h = ReLU()(h)
                h = LeakyReLU(alpha=0.1)(h)
                if self.drop_rate > 0.:
                    h = Dropout(rate=self.drop_rate)(h)

        # Fully Connected Layer
        if fully_connected is True:
            conv_shape = tuple(h.shape[1:])
            h = Flatten()(h)
            flatten_shape = h.shape[1]
            h = Dense(units=flatten_shape, activation=self.activation, use_bias=self.use_bias)(h)
            h = Dense(units=int(flatten_shape / 10), activation=self.activation, use_bias=self.use_bias)(h)
            h = Dense(units=flatten_shape, activation=self.activation, use_bias=self.use_bias)(h)
            h = Reshape(target_shape=conv_shape)(h)

        # Decoder
        for i in range(depth - 2, -1, -1):
            needed_shape = layer_shapes[i]

            if max_pooling is True:
                h = Conv2D(int(2 ** i * filter_root), kernel_size, activation=self.activation, padding='same',
                           use_bias=self.use_bias, strides=strides, **kwargs)(h)
                h = UpSampling2D(size=pool_size)(h)
            elif max_pooling is False:
                h = Conv2DTranspose(int(2 ** i * filter_root), kernel_size, activation=self.activation,
                                    padding='same',
                                    use_bias=self.use_bias, strides=strides, **kwargs)(h)
            h = BatchNormalization()(h)
            #h = ReLU()(h)
            h = LeakyReLU(alpha=0.1)(h)
            if self.drop_rate > 0.:
                h = Dropout(rate=self.drop_rate)(h)


            # Crop network and add skip connections
            crop = cropping_layer(needed_shape, is_shape=(h.shape[1], h.shape[2]))
            h = Cropping2D(cropping=(crop[0], crop[1]))(h)
            h = Add()([convs[i], h])

            h = Conv2D(int(2 ** i * filter_root), kernel_size, activation=self.activation, padding='same',
                       use_bias=self.use_bias, **kwargs)(h)
            h = BatchNormalization()(h)
            #h = ReLU()(h)
            h = LeakyReLU(alpha=0.1)(h)
            if self.drop_rate > 0.:
                h = Dropout(rate=self.drop_rate)(h)

        # Output layer
        h = Conv2D(filters=self.channels, kernel_size=(1, 1), activation=self.activation, use_bias=self.use_bias,
                   padding="same", **kwargs)(h)
        h = Softmax()(h)

        # Build model and compile Model
        self.model = TFmodel(input_layer, h)
        self.model.compile(optimizer=self.optimizer, loss=self.loss, metrics=['accuracy'])


    def summarize(self):
        self.model.summary()

    def save_config(self, pathname="./config", filename=None):
        # Check and create directory
        if not os.path.exists(pathname):
            os.makedirs(pathname)

        if filename:
            settings_filename = "{}/{}.config".format(pathname, filename)
        else:
            self.now = datetime.now()
            self.now_str = self.now.strftime("%d-%m-%Y_%H-%M-%S")
            settings_filename = "{}/{}_stft.config".format(pathname, self.now_str)

        # Write all important parameters to config file
        if isinstance(self.optimizer, str) is True:
            optimizer_name = self.optimizer
        else:
            try:
                optimizer_name = self.optimizer.name
            except AttributeError:
                optimizer_name = self.optimizer._name

        config_dict = dict(shape=self.shape, ts_length=self.ts_length, dt=self.dt_orig, channels=self.channels,
                           depth=self.depth, filter_root=self.filter_root, kernel_size=self.kernel_size,
                           strides=self.strides, optimizer=optimizer_name, fully_connected=self.fully_connected,
                           use_bias=self.use_bias, loss=self.loss, activation=self.activation,
                           drop_rate=self.drop_rate, decimation_factor=self.decimation_factor,
                           max_pooling=self.max_pooling, kwargs=self.kwargs,
                           data_augmentation=self.data_augmentation)
        save_obj(dictionary=config_dict, filename=settings_filename)
        print("Save config file as {}".format(settings_filename))

    def save_model(self, pathname_model="./Models", pathname_config="./config", filename=None):
        """
        Save model as .h5 file and write a .txt file with all important settings.
        """
        # Check whether pathname exists, otherwise create new directories
        if not os.path.exists(pathname_model):
            os.makedirs(pathname_model)

        if not os.path.exists(pathname_config):
            os.makedirs(pathname_config)

        # Save config file
        self.save_config(pathname=pathname_config, filename=filename)
        # Save fully trained model
        #  If checkpoints are available, the model is saved from the latest checkpoint to prevent overfitting
        for callback_index, callback_val in enumerate(self.callbacks):
            if type(callback_val) == tf.keras.callbacks.ModelCheckpoint:
                self.model.load_weights(self.callbacks[callback_index].filepath)
                print("Model is saved from latest checkpoints.")
                break

        if filename:
            self.model.save("{}/{}.h5".format(pathname_model, filename), overwrite=True)
            print("Saved Model as {}/{}.h5".format(pathname_model, filename))
        else:
            self.model.save("{}/{}_stft.h5".format(pathname_model, self.now_str), overwrite=True)
            print("Saved Model as {}/{}_stft.h5".format(pathname_model, self.now_str))


    def train_model_generator(self, signal_file, noise_file,
                              epochs=50, batch_size=20, validation_split=0.15, verbose=1,
                              workers=8, use_multiprocessing=True, max_queue_size=10):

        # Save config file in config directory as tmp.config
        filename_tmp_config = "{}_{}_tmp".format(self.now_str, "stft")
        self.save_config(pathname="./config", filename=filename_tmp_config)

        # Split value to split data into training and validation datasets
        split = int(len(signal_file) * (1 - validation_split))

        # Shuffle list randomly to get different data for validation
        if self.shuffle is True:
            random.shuffle(signal_file)

        # Generate data for each batch
        generator_train = DataGenerator(signal_list=signal_file[:split], noise_list=noise_file,
                                        batch_size=batch_size, channels=self.channels,
                                        shape=self.shape, data_augmentation=self.data_augmentation,
                                        dt=self.dt_orig, ts_length=self.ts_length,
                                        decimation_factor=self.decimation_factor,
                                        **self.kwargs)
        generator_validate = DataGenerator(signal_list=signal_file[split:], noise_list=noise_file,
                                           batch_size=batch_size, channels=self.channels,
                                           shape=self.shape, data_augmentation=self.data_augmentation,
                                           dt=self.dt_orig, ts_length=self.ts_length,
                                           decimation_factor=self.decimation_factor,
                                           **self.kwargs)

        self.history = self.model.fit(x=generator_train, epochs=epochs, workers=workers,
                                      use_multiprocessing=use_multiprocessing,
                                      verbose=verbose, validation_data=generator_validate,
                                      callbacks=self.callbacks, max_queue_size=max_queue_size)

        # Remove temporary config file
        os.remove("./config/{}.config".format(filename_tmp_config))

    def plot_history(self, pathname="./figures", plot=True, filename=None):
        """
        Plot loss vs epochs of training and validation
        """
        # Create directory
        if not os.path.exists(pathname):
            os.makedirs(pathname)

        # Create name for figure
        if filename:
            name = filename
        else:
            name = self.now_str

        # summarize history for accuracy
        fig_acc = plt.figure()
        plt.plot(self.history.history['accuracy'])
        plt.plot(self.history.history['val_accuracy'])
        plt.title('model accuracy')
        plt.ylabel('accuracy')
        plt.xlabel('epoch')
        plt.legend(['train', 'test'], loc='upper left')

        if plot is True:
            plt.savefig("{}/{}_accuracy.png".format(pathname, name))

        # summarize history for loss
        fig_loss = plt.figure()
        plt.plot(self.history.history['loss'])
        plt.plot(self.history.history['val_loss'])
        plt.title('model loss')
        plt.ylabel('loss')
        plt.xlabel('epoch')
        plt.legend(['train', 'test'], loc='upper left')

        if plot is True:
            plt.savefig("{}/{}_loss.png".format(pathname, name))

        if plot is not True:
            return fig_acc, fig_loss


class DataGenerator(Sequence):

    def __init__(self, signal_list, noise_list, batch_size=20, shape=(90, 6001), channels=2, decimation_factor=2,
                 dt=1.0, ts_length=6001, data_augmentation=True, **kwargs):

        self.shape = shape
        self.batch_size = batch_size
        self.signal_list = signal_list
        self.noise_list = glob.glob(noise_list)
        self.channels = channels
        self.decimation_factor = decimation_factor
        self.dt = dt
        self.ts_length = ts_length
        self.data_augmentation = data_augmentation
        self.kwargs = kwargs

        if len(self.noise_list) == 0:
            msg = "Could not load noise files from {}".format(self.noise_list)
            raise ValueError(msg)

        if len(self.signal_list) == 0:
            msg = "Could not load noise files from {}".format(self.signal_list)
            raise ValueError(msg)

    def __len__(self):
        return int(np.floor(len(self.signal_list) / self.batch_size))

    def __getitem__(self, index):
        """Generate one batch of data"""
        # Generate data
        return self.__data_generation()

    def __data_generation(self):
        X = np.empty(shape=(self.batch_size, *self.shape, self.channels), dtype="float16")   # Empty input data
        Y = np.empty(shape=(self.batch_size, *self.shape, self.channels), dtype="float16")   # Empty target data

        for i in range(self.batch_size):
            # XXX Add warning when while loop is infinte and signal length is to short!
            # Read signal
            len_signal = 0
            while len_signal < self.ts_length:
                signal_filename = "{}".format(self.signal_list[random.randint(0, len(self.signal_list) - 1)])
                signal = np.load(signal_filename)
                len_signal = len(signal['data'])

            # Read noise
            # Proof noise for correct length and check whether array does not contain to many zeros
            len_noise = 0
            while len_noise < self.ts_length:
                noise_filename = "{}".format(self.noise_list[random.randint(0, len(self.noise_list) - 1)])
                try:
                    noise = np.load(noise_filename)
                except ValueError:
                    msg = f"Numpy cannot load {noise_filename}.\n" \
                          f"The file seems to have an internal error."
                    raise ValueError(msg)

                len_noise = len(noise['data'])
                # Check how many percent zeros contains the noise array
                if np.count_nonzero(np.diff(noise['data'])) / len(noise['data']) < 0.95:
                    len_noise = 0

            # XXX Leads to Runtime Warnings (Division by zero) in estimation of mapping functions
            # XXX RuntimeWarning: divide by zero encountered in true_divide
            # DATA AUGMENTATION
            # Move signal randomly, hence P-arrival varies its place
            # Add randomly zeros at beginning
            if self.data_augmentation is True:
                # Read signal and noise from npz files
                try:
                    p_samp = signal["itp"]  # Sample of P-arrival
                    s_samp = signal["its"]  # Sample of S-arrival
                except KeyError:
                    p_samp = None
                    s_samp = None

                # Read data arrays from signal and noise
                signal = signal["data"]
                noise = noise["data"][:self.ts_length]

                # epsilon = 0  # Avoiding zeros in added arrays
                # shift1 = np.random.uniform(low=-1, high=1, size=int(self.ts_length - s_samp)) * epsilon
                if p_samp and s_samp:
                    if int(self.ts_length - s_samp) < 0:
                        shift1 = np.zeros(0)
                    else:
                        shift1 = np.zeros(shape=int(self.ts_length - s_samp))
                    signal = np.concatenate((shift1, signal))
                    # Cut signal to length of ts_length and arrival of P-phase is included
                    p_samp += len(shift1)
                    s_samp += len(shift1)
                    start = random.randint(0, p_samp)
                    signal = signal[start:start + self.ts_length]
                else:                                                # XXX Add case just for p_samp
                    if self.ts_length > len(signal):
                        start = random.randint(0, len(signal) - self.ts_length - 1)
                        signal = signal[start:int(start + self.ts_length)]
                    else:
                        signal = signal[:self.ts_length]
            else:
                signal = signal[:self.ts_length]

            # Preprocess data and get sampling rate for time-frequency representation
            # XXX Preprocessing as parameters
            noise, _ = preprocessing(noise, dt=self.dt, decimation_factor=self.decimation_factor)
                                     # filter=dict(type="bandpass", freqmin=0.03, freqmax=0.5))
                                     # taper=dict(max_percentage=0.02, type="cosine"))
            signal, self.dt_tf = preprocessing(signal, dt=self.dt, decimation_factor=self.decimation_factor)
                                               # filter=dict(type="bandpass", freqmin=0.03, freqmax=0.5))
                                               # taper=dict(max_percentage=0.02, type="cosine"))

            # Normalize Noise and signal by each max. absolute value
            # Since noise and signal do not have same amplitude range, each trace is normalized by itself
            noise = noise / np.max(np.abs(noise))
            signal = signal / np.max(np.abs(signal))

            # Adding signal and noise
            rand_noise = np.random.uniform(0, 2)
            rand_signal = np.random.uniform(0, 2)
            signal *= rand_signal
            noise *= rand_noise
            noisy_signal = signal + noise

            # Normalize Signal and Noise
            norm = np.max(np.abs(noisy_signal))
            noisy_signal = noisy_signal / norm
            signal = signal / norm
            noise = noise / norm

            # STFT of noisy signal and signal
            _, _, cns = stft(noisy_signal, fs=1 / self.dt_tf, **self.kwargs)
            _, _, cs = stft(signal, fs=1 / self.dt_tf, **self.kwargs)
            _, _, cn = stft(noise, fs=1 / self.dt_tf, **self.kwargs)


            np.seterr(divide='ignore', invalid='ignore')  # Ignoring Runtime Warnings for division by zero
            # Write data to empty np arrays
            # Zhu et al, 2018
            X[i, :, :, 0] = cns.real / np.max(np.abs(cns.real))
            Y[i, :, :, 0] = 1 / (1 + np.abs(cn) / np.abs(cs))

            # Replace nan and inf values
            Y[i, :, :, 0] = np.nan_to_num(Y[i, :, :, 0])

            if self.channels == 2:
                # Zhu et al, 2018
                X[i, :, :, 1] = cns.imag / np.max(np.abs(cns.imag))
                Y[i, :, :, 1] = (np.abs(cn) / np.abs(cs)) / (1 + np.abs(cn) / np.abs(cs))

                # Replace nan and inf values
                Y[i, :, :, 1] = np.nan_to_num(Y[i, :, :, 1])
            elif self.channels > 2:
                msg = "Channel number cannot exceed 2.\nYour number of channels is {}".format(self.channels)
                raise ValueError(msg)

        return X, Y
