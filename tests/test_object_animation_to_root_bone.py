"""Run with Blender 4.5+:

blender --background --factory-startup --python tests/test_object_animation_to_root_bone.py
"""

import importlib.util
from pathlib import Path

import bpy
from mathutils import Euler, Matrix


ADDON_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "object_animation_to_root_bone.py"
)


def load_addon():
    spec = importlib.util.spec_from_file_location(
        "object_animation_to_root_bone_tested",
        ADDON_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register()
    return module


def assert_matrix_close(actual, expected, tolerance=4.0e-4):
    error = max(
        abs(float(actual[row][column]) - float(expected[row][column]))
        for row in range(4)
        for column in range(4)
    )
    assert error <= tolerance, f"matrix error {error} exceeds {tolerance}"


def add_curve(channelbag, data_path, index, keys):
    curve = channelbag.fcurves.new(data_path, index=index)
    for frame, value in keys:
        point = curve.keyframe_points.insert(frame, value)
        point.interpolation = 'BEZIER'
    curve.update()
    return curve


def create_scene(suffix="", root_rotation_mode='QUATERNION'):
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for selected in bpy.context.selected_objects:
        selected.select_set(False)

    collection = bpy.data.collections.new(f"ObjectToRootTest{suffix}")
    bpy.context.scene.collection.children.link(collection)

    armature = bpy.data.armatures.new(f"TransferRigData{suffix}")
    obj = bpy.data.objects.new(f"TransferRig{suffix}", armature)
    collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    bpy.ops.object.mode_set(mode='EDIT')
    root = armature.edit_bones.new("Root")
    root.head = (0.2, -0.1, 0.3)
    root.tail = (0.2, 0.9, 0.3)

    child = armature.edit_bones.new("Hips")
    child.head = (0.2, 0.9, 0.3)
    child.tail = (0.2, 1.9, 0.3)
    child.parent = root
    child.use_connect = False
    bpy.ops.object.mode_set(mode='POSE')

    armature.bones.active = armature.bones["Root"]
    obj.pose.bones["Root"].select = True
    obj.rotation_mode = 'QUATERNION'
    obj.pose.bones["Root"].rotation_mode = root_rotation_mode

    action = bpy.data.actions.new(f"CombinedObjectAndPoseMotion{suffix}")
    slot = action.slots.new('OBJECT', obj.name)
    layer = action.layers.new("Animation")
    strip = layer.strips.new(type='KEYFRAME')
    channelbag = strip.channelbags.new(slot)
    action.use_frame_range = True
    action.frame_start = 1.0
    action.frame_end = 5.0

    locations = (
        (0.0, 0.0, 0.0),
        (1.25, -0.5, 0.4),
        (2.5, 0.2, 0.8),
    )
    object_rotations = (
        Euler((0.0, 0.0, 0.0)).to_quaternion(),
        Euler((0.05, -0.1, 0.35)).to_quaternion(),
        Euler((0.1, 0.15, 0.7)).to_quaternion(),
    )
    for index in range(3):
        add_curve(
            channelbag,
            "location",
            index,
            [
                (frame, locations[key_index][index])
                for key_index, frame in enumerate((1.0, 3.0, 5.0))
            ],
        )
    for index in range(4):
        add_curve(
            channelbag,
            "rotation_quaternion",
            index,
            [
                (frame, object_rotations[key_index][index])
                for key_index, frame in enumerate((1.0, 3.0, 5.0))
            ],
        )

    root_path = 'pose.bones["Root"]'
    root_locations = (
        (0.0, 0.0, 0.0),
        (0.15, 0.3, -0.05),
        (0.3, 0.6, 0.0),
    )
    root_rotations = (
        Euler((0.0, 0.0, 0.0)).to_quaternion(),
        Euler((0.2, 0.05, -0.15)).to_quaternion(),
        Euler((0.4, -0.1, -0.3)).to_quaternion(),
    )
    for index in range(3):
        add_curve(
            channelbag,
            f"{root_path}.location",
            index,
            [
                (frame, root_locations[key_index][index])
                for key_index, frame in enumerate((1.0, 3.0, 5.0))
            ],
        )
    if root_rotation_mode == 'QUATERNION':
        for index in range(4):
            add_curve(
                channelbag,
                f"{root_path}.rotation_quaternion",
                index,
                [
                    (frame, root_rotations[key_index][index])
                    for key_index, frame in enumerate((1.0, 3.0, 5.0))
                ],
            )
    else:
        root_eulers = tuple(
            rotation.to_euler(root_rotation_mode) for rotation in root_rotations
        )
        for index in range(3):
            add_curve(
                channelbag,
                f"{root_path}.rotation_euler",
                index,
                [
                    (frame, root_eulers[key_index][index])
                    for key_index, frame in enumerate((1.0, 3.0, 5.0))
                ],
            )

    hips_path = 'pose.bones["Hips"].location'
    add_curve(
        channelbag,
        hips_path,
        2,
        [(1.0, 0.0), (3.0, 0.25), (5.0, 0.0)],
    )

    animation_data = obj.animation_data_create()
    animation_data.action = action
    animation_data.action_slot = slot
    return obj, action


def evaluated_bone_world(obj, bone_name, frame):
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.matrix_world @ evaluated.pose.bones[bone_name].matrix


def run_transfer_case(addon):
    obj, action = create_scene()
    frames = tuple(range(1, 6))
    expected_root = {
        frame: evaluated_bone_world(obj, "Root", frame) for frame in frames
    }
    expected_child = {
        frame: evaluated_bone_world(obj, "Hips", frame) for frame in frames
    }

    bpy.context.scene.frame_set(2)
    summary = addon.transfer_object_animation_to_root(
        bpy.context,
        obj,
        "Root",
        target_mode='IDENTITY',
        create_backup=True,
    )

    assert summary["action_name"] == action.name
    assert summary["frame_count"] == 5
    assert bpy.context.scene.frame_current == 2
    assert obj.pose.bones["Root"].rotation_mode == 'QUATERNION'

    for frame in frames:
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        assert_matrix_close(obj.matrix_world, Matrix.Identity(4))
        assert_matrix_close(
            evaluated_bone_world(obj, "Root", frame),
            expected_root[frame],
        )
        assert_matrix_close(
            evaluated_bone_world(obj, "Hips", frame),
            expected_child[frame],
        )

    slot = obj.animation_data.action_slot
    bags = addon._channelbags_for_slot(action, slot)
    paths = {
        curve.data_path
        for fcurves, _groups, _is_layered in bags
        for curve in fcurves
    }
    assert not (paths & addon._OBJECT_TRANSFORM_PATHS)
    root_paths = addon._bone_transform_paths("Root")
    assert 'pose.bones["Root"].rotation_quaternion' in root_paths & paths
    assert any(
        candidate.get(addon._BACKUP_FLAG, False)
        and candidate.get(addon._BACKUP_SOURCE) == action.name
        for candidate in bpy.data.actions
    )


def run_operator_case(addon):
    obj = bpy.data.objects["TransferRig"]
    bpy.context.view_layer.objects.active = obj
    if obj.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')
    obj.data.bones.active = obj.data.bones["Root"]

    # The first conversion intentionally removes all object transform curves,
    # so invoking it again must fail safely without changing the Action.
    try:
        bpy.ops.anim.object_animation_to_active_root()
    except RuntimeError as error:
        assert "no armature-object transform curves" in str(error)
    else:
        raise AssertionError("The second conversion should have been rejected")


def run_keep_current_case(addon):
    obj, _action = create_scene("Current", root_rotation_mode='XYZ')
    frames = tuple(range(1, 6))
    expected_root = {
        frame: evaluated_bone_world(obj, "Root", frame) for frame in frames
    }

    bpy.context.scene.frame_set(2)
    bpy.context.view_layer.update()
    expected_object_world = obj.matrix_world.copy()
    addon.transfer_object_animation_to_root(
        bpy.context,
        obj,
        "Root",
        target_mode='CURRENT',
        create_backup=False,
    )
    assert obj.pose.bones["Root"].rotation_mode == 'XYZ'

    for frame in frames:
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        assert_matrix_close(obj.matrix_world, expected_object_world)
        assert_matrix_close(
            evaluated_bone_world(obj, "Root", frame),
            expected_root[frame],
        )


def main():
    addon = load_addon()
    try:
        run_transfer_case(addon)
        run_operator_case(addon)
        run_keep_current_case(addon)
        print("object_animation_to_root_bone: all tests passed")
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
