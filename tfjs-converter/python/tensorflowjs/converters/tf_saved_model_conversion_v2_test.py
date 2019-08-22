# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Unit tests for artifact conversion to and from Tensorflow SavedModel v2."""

import base64
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import tensorflow as tf
from tensorflow.python.eager import def_function
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import importer
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_spec
from tensorflow.python.framework import test_util
from tensorflow.python.ops import gen_nn_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import variables
from tensorflow.python.training.tracking import tracking
from tensorflow.python.saved_model.save import save
import tensorflow_hub as hub
from tensorflowjs import version
from tensorflowjs.converters import tf_saved_model_conversion_v2

SAVED_MODEL_DIR = 'saved_model'
HUB_MODULE_DIR = 'hub_module'

class ConvertTest(tf.test.TestCase):
  def setUp(self):
    super(ConvertTest, self).setUp()
    self._tmp_dir = tempfile.mkdtemp()

  def tearDown(self):
    if os.path.isdir(self._tmp_dir):
      shutil.rmtree(self._tmp_dir)
    super(ConvertTest, self).tearDown()

  def _create_saved_model_v1(self):
    """Create a TensorFlow SavedModel for testing."""

    graph = tf.Graph()
    with graph.as_default():
      x = tf.compat.v1.constant([[37.0, -23.0], [1.0, 4.0]])
      w = tf.compat.v1.get_variable('w', shape=[2, 2])
      y = tf.compat.v1.matmul(x, w)
      output = tf.compat.v1.nn.softmax(y)
      init_op = w.initializer

      # Create a builder.
      save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
      builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(save_dir)

      with tf.compat.v1.Session() as sess:
        # Run the initializer on `w`.
        sess.run(init_op)

        builder.add_meta_graph_and_variables(
            sess, [tf.compat.v1.saved_model.tag_constants.SERVING],
            signature_def_map={
                "serving_default":
                    tf.compat.v1.saved_model \
                        .signature_def_utils.predict_signature_def(
                            inputs={"x": x},
                            outputs={"output": output})
            },
            assets_collection=None)

      builder.save()

  def _create_saved_model_with_fusable_conv2d(self):
    """Test a basic model with fusable conv2d."""
    layers = [
        tf.keras.layers.Conv2D(
            16, [3, 3], padding='same', use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU()
    ]
    model = tf.keras.Sequential(layers)
    model.predict(tf.ones((1, 224, 224, 3)))
    tf.keras.backend.set_learning_phase(0)
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf.saved_model.save(model, save_dir)

  def _create_saved_model(self):
    """Test a basic model with functions to make sure functions are inlined."""
    input_data = constant_op.constant(1., shape=[1])
    root = tracking.AutoTrackable()
    root.v1 = variables.Variable(3.)
    root.v2 = variables.Variable(2.)
    root.f = def_function.function(lambda x: root.v1 * root.v2 * x)
    to_save = root.f.get_concrete_function(input_data)

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_control_flow(self):
    """Test a basic model with control flow to inlined."""
    @tf.function
    def find_next_odd(v):
      v1 = v + 1
      while tf.equal(v1 % 2, 0):
        v1 = v1 + 1
      return v1
    root = tracking.AutoTrackable()
    root.f = find_next_odd
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([], dtypes.int32))

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_unsupported_saved_model(self):
    root = tracking.AutoTrackable()
    root.w = variables.Variable(tf.random.uniform([2, 2]))

    @def_function.function
    def exported_function(x):
      root.x = constant_op.constant([[37.0, -23.0], [1.0, 4.0]])
      root.y = tf.matmul(root.x, root.w)
      # unsupported op: linalg.diag
      root.z = tf.linalg.diag(root.y)
      return root.z * x

    root.f = exported_function
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([], dtypes.float32))

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_debug_ops(self):
    root = tracking.AutoTrackable()
    root.w = variables.Variable(tf.random.uniform([2, 2]))

    @def_function.function
    def exported_function(x):
      root.x = constant_op.constant([[37.0, -23.0], [1.0, 4.0]])
      root.y = tf.matmul(root.x, root.w)
      tf.compat.v1.Print(root.x, [root.x])
      tf.compat.v1.Assert(tf.greater(tf.reduce_max(root.x), 0), [root.x])
      tf.compat.v1.check_numerics(root.x, 'NaN found')
      return root.y * x

    root.f = exported_function
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([], dtypes.float32))

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_hub_module(self):
    # Module function that doubles its input.
    def double_module_fn():
      w = tf.Variable([2.0, 4.0])
      x = tf.compat.v1.placeholder(dtype=tf.float32)
      hub.add_signature(inputs=x, outputs=x*w)
    graph = tf.Graph()
    with graph.as_default():
      spec = hub.create_module_spec(double_module_fn)
      m = hub.Module(spec)
    # Export the module.
    with tf.compat.v1.Session(graph=graph) as sess:
      sess.run(tf.compat.v1.global_variables_initializer())
      m.export(os.path.join(self._tmp_dir, HUB_MODULE_DIR), sess)

  def test_convert_saved_model_v1(self):
    self._create_saved_model_v1()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    weights = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [{'dtype': 'float32', 'name': 'w', 'shape': [2, 2]}]}]

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    weights_manifest = model_json['weightsManifest']
    self.assertEqual(weights_manifest, weights)
    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model(self):
    self._create_saved_model()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    weights = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [{'dtype': 'float32',
                     'name': 'StatefulPartitionedCall/mul',
                     'shape': []}]}]

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    weights_manifest = model_json['weightsManifest']
    self.assertEqual(len(weights_manifest), len(weights))
    if sys.version_info[0] < 3:
      self.assertItemsEqual(weights_manifest[0]['paths'],
                            weights[0]['paths'])
      self.assertItemsEqual(weights_manifest[0]['weights'],
                            weights[0]['weights'])
    else:
      self.assertCountEqual(weights_manifest[0]['paths'],
                            weights[0]['paths'])
      self.assertCountEqual(weights_manifest[0]['weights'],
                            weights[0]['weights'])

  def test_convert_saved_model_with_fused_conv2d(self):
    self._create_saved_model_with_fusable_conv2d()
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
      print(model_json)
    self.assertTrue(model_json['modelTopology'])
    nodes = model_json['modelTopology']['node']

    fusedOp = None
    for node in nodes:
      self.assertTrue(not 'BatchNorm' in node['op'])
      self.assertTrue(not 'Relu' in node['op'])
      self.assertTrue(not 'BiasAdd' in node['op'])
      if node['op'] == '_FusedConv2D':
        fusedOp = node
    self.assertTrue(fusedOp is not None)
    self.assertEqual(
        base64.b64decode(fusedOp['attr']['fused_ops']['list']['s'][0]),
        b'BiasAdd')
    self.assertEqual(
        base64.b64decode(fusedOp['attr']['fused_ops']['list']['s'][1]),
        b'Relu')

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_control_flow(self):
    self._create_saved_model_with_control_flow()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    weights = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [{'dtype': 'int32', 'shape': [],
                     'name': 'StatefulPartitionedCall/while/loop_counter'},
                    {'dtype': 'int32', 'shape': [],
                     'name': 'StatefulPartitionedCall/while/maximum_iterations'
                    },
                    {'dtype': 'int32', 'shape': [],
                     'name': 'StatefulPartitionedCall/while/cond/_3/mod/y'},
                    {'dtype': 'int32', 'shape': [],
                     'name': 'StatefulPartitionedCall/while/cond/_3/Equal/y'},
                    {'dtype': 'int32', 'shape': [],
                     'name': 'StatefulPartitionedCall/while/body/_4/add_1/y'},
                    {'name': 'StatefulPartitionedCall/add/y',
                     'dtype': 'int32', 'shape': []}]}]

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    weights_manifest = model_json['weightsManifest']
    self.assertEqual(len(weights_manifest), len(weights))
    if sys.version_info[0] < 3:
      self.assertItemsEqual(weights_manifest[0]['paths'],
                            weights[0]['paths'])
      self.assertItemsEqual(weights_manifest[0]['weights'],
                            weights[0]['weights'])
    else:
      self.assertCountEqual(weights_manifest[0]['paths'],
                            weights[0]['paths'])
      self.assertCountEqual(weights_manifest[0]['weights'],
                            weights[0]['weights'])

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_optimizer_add_unsupported_op(self):
    self._create_unsupported_saved_model()
    with self.assertRaisesRegexp(  # pylint: disable=deprecated-method
        ValueError, r'^Unsupported Ops'):
      tf_saved_model_conversion_v2.convert_tf_saved_model(
          os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
          os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
      )

  def test_convert_saved_model_skip_op_check(self):
    self._create_unsupported_saved_model()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR), skip_op_check=True
    )

    weights = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [{'dtype': 'float32',
                     'name': 'StatefulPartitionedCall/MatrixDiag',
                     'shape': [2, 2, 2]}]}]
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    weights_manifest = model_json['weightsManifest']
    self.assertEqual(weights_manifest, weights)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  # (TODO: piyu) disable this test, need to change
  # convert_variables_to_constants_v2 to set function_optimization=aggressive.
  @unittest.skip('not supported')
  def test_convert_saved_model_strip_debug_ops(self):
    self._create_saved_model_with_debug_ops()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        strip_debug_ops=True)

    weights = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [{
            'dtype': 'float32',
            'name': 'add',
            'shape': [2, 2]
        }]
    }]
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    weights_manifest = model_json['weightsManifest']
    self.assertEqual(weights_manifest, weights)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_hub_module_v1(self):
    self._create_hub_module()
    module_path = os.path.join(self._tmp_dir, HUB_MODULE_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    tf_saved_model_conversion_v2.convert_tf_hub_module(module_path, tfjs_path)

    weights = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [{
            'shape': [2],
            'name': 'module/Variable',
            'dtype': 'float32'
        }]
    }]

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])

    weights_manifest = model_json['weightsManifest']
    self.assertEqual(weights_manifest, weights)

    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_hub_module_v2(self):
    self._create_saved_model()
    module_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    tf_saved_model_conversion_v2.convert_tf_hub_module(
        module_path, tfjs_path, "serving_default", "serve")

    weights = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [{
            'shape': [],
            'name': 'StatefulPartitionedCall/mul',
            'dtype': 'float32'
        }]
    }]

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])

    weights_manifest = model_json['weightsManifest']
    self.assertEqual(weights_manifest, weights)

    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def testFoldBatchNorms(self):
    with tf.compat.v1.Session() as sess:
      inputs = [1, 4, 2, 5, 3, 6, -1, -4, -2, -5, -3, -6]
      input_op = constant_op.constant(
          np.array(inputs), shape=[1, 1, 6, 2], dtype=dtypes.float32)
      weights = [1, 2, 3, 4, 0.1, 0.2, 0.3, 0.4]
      weights_op = constant_op.constant(
          np.array(weights), shape=[1, 2, 2, 2], dtype=dtypes.float32)
      conv_op = nn_ops.conv2d(
          input_op, weights_op, [1, 1, 1, 1], padding="SAME", name="conv_op")
      mean_op = constant_op.constant(
          np.array([10, 20]), shape=[2], dtype=dtypes.float32)
      variance_op = constant_op.constant(
          np.array([0.25, 0.5]), shape=[2], dtype=dtypes.float32)
      beta_op = constant_op.constant(
          np.array([0.1, 0.6]), shape=[2], dtype=dtypes.float32)
      gamma_op = constant_op.constant(
          np.array([1.0, 2.0]), shape=[2], dtype=dtypes.float32)
      test_util.set_producer_version(ops.get_default_graph(), 8)
      gen_nn_ops._batch_norm_with_global_normalization(
          conv_op,
          mean_op,
          variance_op,
          beta_op,
          gamma_op,
          0.00001,
          False,
          name="output")
      original_graph_def = sess.graph_def
      original_result = sess.run(["output:0"])
    optimized_graph_def = tf_saved_model_conversion_v2.fold_batch_norms(
        original_graph_def)
    with tf.compat.v1.Session() as sess:
      _ = importer.import_graph_def(
          optimized_graph_def, input_map={}, name="optimized")
      optimized_result = sess.run(["optimized/output:0"])

    self.assertAllClose(original_result, optimized_result)

    for node in optimized_graph_def.node:
      self.assertNotEqual("BatchNormWithGlobalNormalization", node.op)

if __name__ == '__main__':
  tf.test.main()
