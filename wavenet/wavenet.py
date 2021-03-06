import tensorflow as tf
import numpy as np
from auxilaries import reader, utils
from wavenet import masked, loss_func


DEFAULT_LR_SCHEDULE = {
    0: 2e-4,
    90000: 4e-4 / 3,
    120000: 6e-5,
    150000: 4e-5,
    180000: 2e-5,
    210000: 6e-6,
    240000: 2e-6}
################################################################
# use resize_conv1d instead of trans_conv1d, also change parallel wavenet.
DETAIL_LOG = True


class WNHelper(object):
    """wavenet helper"""

    @staticmethod
    def upsample_conv1d(
            x, num_filters, filter_length, stride,
            use_resize_conv, name_patt, act='tanh',
            use_weight_norm=False, init=False):
        act_func = masked.get_upsample_act(act)
        conv_args = {"x": x,
                     "num_filters": num_filters,
                     "filter_length": filter_length,
                     "stride": stride,
                     "activation": act_func,
                     "use_weight_norm": use_weight_norm,
                     "init": init}
        if use_resize_conv:
            y = masked.resize_conv1d(
                **conv_args, name=name_patt.format("resize_conv"))
        else:
            y = masked.trans_conv1d(
                **conv_args, name=name_patt.format("trans_conv"))
        return y


def _deconv_stack(inputs, width, config, use_resize_conv,
                  act='tanh', name='',
                  use_weight_norm=False, init=False):
    b, l, _ = inputs.get_shape().as_list()
    frame_shift = int(np.prod([c[1] for c in config]))
    mel_en = inputs
    for i in range(len(config)):
        fl, s = config[i]
        if name:
            tc_name = '{}/{{}}_{:d}'.format(name, i + 1)
        else:
            tc_name = '{{}}_{:d}'.format(i + 1)
        mel_en = WNHelper.upsample_conv1d(
            mel_en,
            num_filters=width,
            filter_length=fl,
            stride=s,
            use_resize_conv=use_resize_conv,
            name_patt=tc_name,
            act=act,
            use_weight_norm=use_weight_norm,
            init=init)
        if DETAIL_LOG:
            # When using tanh, the spike of the histogram should be at 0.
            hist_name = '{}/mel_en_{}'.format(name, i) if name else 'mel_en_{}'.format(i)
            tf.summary.histogram(hist_name, mel_en)
    mel_en.set_shape([b, l * frame_shift, width])
    return mel_en


def _condition(x, cond):
    x_len = x.get_shape().as_list()[1]
    cond_len = cond.get_shape().as_list()[1]
    assert cond_len >= x_len
    trim_len = cond_len - x_len
    left_tl = int(trim_len // 2)
    cond = tf.slice(cond, [0, left_tl, 0], [-1, cond_len - trim_len, -1])
    s = x + cond
    s.set_shape(x.get_shape().as_list())
    return s


def _get_batch(train_path, batch_size, wave_length):
    assert train_path is not None
    data_train = reader.Dataset(train_path, is_training=True)
    return data_train.get_batch(batch_size, length=wave_length)


class Wavenet(object):
    """Wavenet object that helps manage the graph."""

    def __init__(self, hparams, train_path=None):
        self.hparams = hparams
        self.num_iters = hparams.num_iters
        self.learning_rate_schedule = dict(
            getattr(hparams, 'lr_schedule', DEFAULT_LR_SCHEDULE))
        self.train_path = train_path
        self.wave_length = self.hparams.wave_length

        self.use_weight_norm = getattr(self.hparams, 'use_weight_norm', False)
        self.double_gate_width = getattr(self.hparams, 'double_gate_width', True)
        self.use_resize_conv = getattr(self.hparams, 'use_resize_conv', False)
        self.upsample_act = getattr(self.hparams, 'upsample_act', 'tanh')
        self.dropout_inputs = getattr(self.hparams, 'dropout_inputs', False)
        self.use_input_noise = getattr(self.hparams, 'use_input_noise', False)
        assert not (self.dropout_inputs and self.use_input_noise)
        # only take the variables used both by
        # feed_forward and calculate_loss as class property.
        self.use_mu_law = self.hparams.use_mu_law
        self.loss_type = self.hparams.loss_type
        if self.use_mu_law:
            self.quant_chann = 2 ** 8
        else:
            self.quant_chann = 2 ** 16
        if self.loss_type == 'ce':
            self.out_width = self.quant_chann
        elif self.loss_type == 'mol':
            mol_mix = self.hparams.mol_mix
            self.out_width = mol_mix * 3
        elif self.loss_type == 'gauss':
            self.out_width = 2
        else:
            raise ValueError('[{}] loss is not supported')

    def get_batch(self, batch_size):
        train_path = self.train_path
        wave_length = self.wave_length
        return _get_batch(train_path, batch_size, wave_length)

    def deconv_stack(self, mel_inputs, init=False):
        mel = mel_inputs['mel']
        deconv_width = self.hparams.deconv_width
        deconv_config = self.hparams.deconv_config  # [[l1, s1], [l2, s2]]
        use_weight_norm = self.use_weight_norm
        use_resize_conv = self.use_resize_conv
        upsample_act = self.upsample_act

        mel_en = _deconv_stack(mel, deconv_width, deconv_config,
                               act=upsample_act,
                               use_resize_conv=use_resize_conv,
                               use_weight_norm=use_weight_norm,
                               init=init)
        return {'encoding': mel_en}

    def encode_signal(self, inputs):
        ###
        # Encode the source with 8-bit Mu-Law or just use 16-bit signal.
        ###
        quant_chann = self.quant_chann
        use_mu_law = self.use_mu_law

        x = inputs['wav']
        if use_mu_law:
            x_quantized = utils.mu_law(x)
            x_scaled = tf.cast(x_quantized, tf.float32) / (quant_chann / 2.)
            real_targets = x_scaled
            cate_targets = tf.cast(x_quantized, tf.int32) + tf.cast(quant_chann / 2., tf.int32)
        else:
            x_quantized = utils.cast_quantize(x, quant_chann)
            x_scaled = x
            real_targets = x
            cate_targets = tf.cast(x_quantized, tf.int32) + tf.cast(quant_chann / 2., tf.int32)

        return {'wav_scaled': x_scaled,
                'real_targets': real_targets,
                'cate_targets': cate_targets}

    def feed_forward(self, inputs, init=False):
        """Build the graph for this configuration.

        Args:
          inputs: A dict of inputs. For training, should contain 'wav'.
          init: data dependent initialization.

        Returns:
          A dict of outputs that includes the 'predictions', 'loss', the 'encoding',
          the 'quantized_input', and whatever metrics we want to track for eval.
        """
        use_weight_norm = self.use_weight_norm
        num_stages = self.hparams.num_stages
        num_layers = self.hparams.num_layers
        filter_length = self.hparams.filter_length
        width = self.hparams.width
        skip_width = self.hparams.skip_width
        out_width = self.out_width
        dropout_inputs = self.dropout_inputs
        use_input_noise = self.use_input_noise
        # in parallel wavenet paper, gate width is the same with residual width
        # not double of that.
        gate_width = 2 * width if self.double_gate_width else width

        ###
        # The Transpose Convolution Stack for mel feature.
        ###
        # wavenet inputs <- trans_conv (l2, s2) <- trans_conv (l1, s1) <- mel_ceps
        # win_len: l1 * s2 + (l2 - s2); win_shift: s1 * s2
        # (l1, s1) = (40, 10), (l2, s2) = (80, 20) is a proper configuration.
        # it is almost consistent with mel analysis frame shift (200) and frame length (800).
        mel = inputs['mel']
        ds_dict = self.deconv_stack({'mel': mel}, init=init)
        mel_en = ds_dict['encoding']

        x_scaled = inputs['wav_scaled']
        x_scaled = tf.expand_dims(x_scaled, 2)

        ###
        # The WaveNet Decoder.
        ###
        if use_input_noise and not init:
            x_scaled += tf.random_normal(shape=x_scaled.get_shape(),
                                         mean=0.0, stddev=0.1)
        if dropout_inputs and not init:
            x_scaled = tf.layers.dropout(x_scaled, rate=0.5, training=True)

        l = masked.shift_right(x_scaled)
        l = masked.conv1d(
            l, num_filters=width, filter_length=filter_length, name='startconv',
            use_weight_norm=use_weight_norm, init=init)

        # Set up skip connections.
        s = masked.conv1d(
            l, num_filters=skip_width, filter_length=1, name='skip_start',
            use_weight_norm=use_weight_norm, init=init)

        # Residual blocks with skip connections.
        for i in range(num_layers):
            dilation = 2 ** (i % num_stages)
            d = masked.conv1d(
                l,
                num_filters=gate_width,
                filter_length=filter_length,
                dilation=dilation,
                name='dilated_conv_%d' % (i + 1),
                use_weight_norm=use_weight_norm,
                init=init)
            c = masked.conv1d(
                mel_en,
                num_filters=gate_width,
                filter_length=1,
                name='mel_cond_%d' % (i + 1),
                use_weight_norm=use_weight_norm,
                init=init)
            d = _condition(d, c)

            assert d.get_shape().as_list()[2] % 2 == 0
            m = d.get_shape().as_list()[2] // 2
            d_sigmoid = tf.sigmoid(d[:, :, :m])
            d_tanh = tf.tanh(d[:, :, m:])
            d = d_sigmoid * d_tanh

            l += masked.conv1d(
                d, num_filters=width, filter_length=1, name='res_%d' % (i + 1),
                use_weight_norm=use_weight_norm, init=init)
            s += masked.conv1d(
                d, num_filters=skip_width, filter_length=1, name='skip_%d' % (i + 1),
                use_weight_norm=use_weight_norm, init=init)

        s = tf.nn.relu(s)
        s = masked.conv1d(s, num_filters=skip_width, filter_length=1, name='out1',
                          use_weight_norm=use_weight_norm, init=init)
        c = masked.conv1d(mel_en, num_filters=skip_width, filter_length=1, name='mel_cond_out1',
                          use_weight_norm=use_weight_norm, init=init)
        s = _condition(s, c)
        s = tf.nn.relu(s)
        out = masked.conv1d(s, num_filters=out_width, filter_length=1, name='out2',
                            use_weight_norm=use_weight_norm, init=init)

        return {'encoding': mel_en,
                'out_params': out}

    def calculate_loss(self, ff_dict):
        ###
        # Compute the loss.
        ###
        real_targets = ff_dict['real_targets']
        cate_targets = ff_dict['cate_targets']
        out = ff_dict['out_params']
        loss_type = self.loss_type
        if loss_type == 'ce':
            loss = loss_func.ce_loss(out, cate_targets)
        elif loss_type == 'mol':
            quant_chann = self.quant_chann
            loss = loss_func.mol_loss(out, real_targets, quant_chann)
        elif loss_type == 'gauss':
            loss = loss_func.gauss_loss(out, real_targets)
            if DETAIL_LOG:
                mean, std = loss_func.mean_std_from_out_params(out, use_log_scales=True)
                tf.summary.histogram('mean', mean)
                tf.summary.histogram('std', std)
                tf.summary.histogram('log_std', tf.log(std))
        else:
            raise ValueError('[{}] loss is not supported.'.format(loss_type))
        return {'loss': loss}


class Fastgen(object):
    """Configuration object that helps manage the graph."""

    def __init__(self, hparams, batch_size=2):
        """."""
        self.batch_size = batch_size
        self.hparams = hparams

        self.use_weight_norm = getattr(self.hparams, 'use_weight_norm', False)
        self.use_mu_law = self.hparams.use_mu_law
        self.loss_type = self.hparams.loss_type
        self.double_gate_width = getattr(self.hparams, 'double_gate_width', True)
        if self.use_mu_law:
            self.quant_chann = 2 ** 8
        else:
            self.quant_chann = 2 ** 16
        if self.loss_type == 'ce':
            self.out_width = self.quant_chann
        elif self.loss_type == 'mol':
            mol_mix = self.hparams.mol_mix
            self.out_width = mol_mix * 3
        elif self.loss_type == 'gauss':
            self.out_width = 2
        else:
            raise ValueError('[{}] loss is not supported')

    # used for data visualization
    def cond_vars(self, inputs):
        num_layers = self.hparams.num_layers
        width = self.hparams.width
        gate_width = 2 * width if self.double_gate_width else width
        skip_width = self.hparams.skip_width
        use_weight_norm = self.use_weight_norm

        mel_en = inputs['encoding']

        cond_var_dict = {}
        for i in range(num_layers):
            cond_layer_name = 'mel_cond_%d' % (i + 1)
            cond_var_dict[cond_layer_name] = masked.conv1d(
                mel_en,
                num_filters=gate_width,
                filter_length=1,
                name=cond_layer_name,
                use_weight_norm=use_weight_norm)
        cond_var_dict['mel_cond_out1'] = masked.conv1d(
            mel_en,
            num_filters=skip_width,
            filter_length=1,
            name='mel_cond_out1',
            use_weight_norm=use_weight_norm)
        return cond_var_dict

    def sample(self, inputs):
        """Build the graph for this configuration.

        Args:
          inputs: A dict of inputs. For training, should contain 'wav'.

        Returns:
          A dict of outputs that includes the 'predictions',
          'init_ops', the 'push_ops', and the 'quantized_input'.
        """
        batch_size = self.batch_size
        num_stages = self.hparams.num_stages
        num_layers = self.hparams.num_layers
        filter_length = self.hparams.filter_length
        width = self.hparams.width
        skip_width = self.hparams.skip_width
        use_mu_law = self.use_mu_law
        use_weight_norm = self.use_weight_norm
        quant_chann = self.quant_chann
        out_width = self.out_width
        deconv_width = self.hparams.deconv_width
        loss_type = self.loss_type
        gate_width = 2 * width if self.double_gate_width else width

        # mel information is trans_conv_stack output, different from wavenet.feed_forward
        mel_en = inputs['encoding']  # [batch_size, deconv_width]
        mel_en = tf.expand_dims(mel_en, 1)  # [batch_size, 1, deconv_width]

        x = inputs['wav']  # [batch_size, 1]
        if use_mu_law:
            # Encode the source with 8-bit Mu-Law.
            x_quantized = utils.mu_law(x)
            x_scaled = tf.cast(x_quantized, tf.float32) / (quant_chann / 2)
        else:
            x_scaled = x
        x_scaled = tf.expand_dims(x_scaled, 2)  # [batch_size, 1, 1]

        init_ops, push_ops = [], []

        ###
        # The WaveNet Decoder.
        ###
        l = x_scaled
        l, inits, pushs = masked.causal_linear(
            x=l,
            n_inputs=1,
            n_outputs=width,
            name='startconv',
            rate=1,
            batch_size=batch_size,
            filter_length=filter_length,
            use_weight_norm=use_weight_norm)

        for init in inits:
            init_ops.append(init)
        for push in pushs:
            push_ops.append(push)

        # Set up skip connections.
        s = masked.linear(l, width, skip_width, name='skip_start',
                          use_weight_norm=use_weight_norm)

        # Residual blocks with skip connections.
        for i in range(num_layers):
            dilation = 2 ** (i % num_stages)

            # dilated masked cnn
            d, inits, pushs = masked.causal_linear(
                x=l,
                n_inputs=width,
                n_outputs=gate_width,
                name='dilated_conv_%d' % (i + 1),
                rate=dilation,
                batch_size=batch_size,
                filter_length=filter_length,
                use_weight_norm=use_weight_norm)

            for init in inits:
                init_ops.append(init)
            for push in pushs:
                push_ops.append(push)

            # local conditioning
            d += masked.linear(
                mel_en, deconv_width, gate_width, name='mel_cond_%d' % (i + 1),
                use_weight_norm=use_weight_norm)

            # gated cnn
            assert d.get_shape().as_list()[2] % 2 == 0
            m = d.get_shape().as_list()[2] // 2
            d = tf.sigmoid(d[:, :, :m]) * tf.tanh(d[:, :, m:])

            # residuals
            l += masked.linear(d, gate_width // 2, width, name='res_%d' % (i + 1),
                               use_weight_norm=use_weight_norm)

            # skips
            s += masked.linear(d, gate_width // 2, skip_width, name='skip_%d' % (i + 1),
                               use_weight_norm=use_weight_norm)

        s = tf.nn.relu(s)
        s = (masked.linear(s, skip_width, skip_width, name='out1',
                           use_weight_norm=use_weight_norm) +
             masked.linear(mel_en, deconv_width, skip_width, name='mel_cond_out1',
                           use_weight_norm=use_weight_norm))
        s = tf.nn.relu(s)
        out = masked.linear(s, skip_width, out_width, name='out2',
                            use_weight_norm=use_weight_norm)  # [batch_size, 1, out_width]

        if loss_type == 'ce':
            sample = loss_func.ce_sample(out, quant_chann)
        elif loss_type == 'mol':
            sample = loss_func.mol_sample(out, quant_chann)
        elif loss_type == 'gauss':
            sample = loss_func.gauss_sample(out, quant_chann)
        else:
            raise ValueError('[{}] loss is not supported.'.format(loss_type))

        return {'init_ops': init_ops,
                'push_ops': push_ops,
                'sample': sample}
