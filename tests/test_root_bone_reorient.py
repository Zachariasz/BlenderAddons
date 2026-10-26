"""Run with Blender 4.5+: blender --background --factory-startup --python this_file.py"""

import importlib.util
import math
from pathlib import Path

import bpy
from mathutils import Euler, Matrix, Vector


ADDON_PATH = Path(__file__).resolve().parents[1] / "addons" / "root_bone_reorient.py"


def load_addon():
    spec = importlib.util.spec_from_file_location("root_bone_reorient_tested", ADDON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register()
    return module


def assert_matrix_close(actual, expected, tolerance=2.0e-5):
    error = max(
        abs(float(actual[row][column]) - float(expected[row][column]))
        for row in range(4)
        for column in range(4)
    )
    assert error <= tolerance, f"matrix error {error} exceeds {tolerance}"


def create_rig(name, root_name, connected_child=False):
    armature = bpy.data.armatures.new(f"{name}Armature")
    obj = bpy.data.objects.new(name, armature)
    bpy.context.scene.collection.objects.link(obj)

    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')

    root = armature.edit_bones.new(root_name)
    root.head = (0.0, 0.0, 0.0)
    root.tail = (0.07321, 0.0, 0.0)
    root.roll = -math.pi

    child = armature.edit_bones.new(f"{root_name}_Child")
    if connected_child:
        child.head = root.tail
        child.tail = (1.07321, 0.0, 0.0)
    else:
        child.head = (0.0, 0.0, 1.0)
        child.tail = (0.0, 0.0, 2.0)
    child.parent = root
    child.use_connect = connected_child

    bpy.ops.object.mode_set(mode='OBJECT')
    armature.bones.active = armature.bones[root_name]
    if "select" in bpy.types.PoseBone.bl_rna.properties:
        obj.pose.bones[root_name].select = True
    else:
        armature.bones[root_name].select = True
    return obj


def add_curve(channelbag, data_path, index, keys):
    fcurve = channelbag.fcurves.new(data_path, index=index)
    for frame, value in keys:
        point = fcurve.keyframe_points.insert(frame, value)
        point.interpolation = 'BEZIER'
    fcurve.update()
    return fcurve


def create_action(obj, root_name, action_name, rotation_mode, offset, cyclic=False):
    action = bpy.data.actions.new(action_name)
    slot = action.slots.new('OBJECT', obj.name)
    layer = action.layers.new("Animation")
    strip = layer.strips.new(type='KEYFRAME')
    channelbag = strip.channelbags.new(slot)
    action.use_frame_range = True
    action.frame_start = 1.0
    action.frame_end = 5.0
    action.use_cyclic = cyclic

    root_path = f'pose.bones["{root_name}"]'
    frames = (1.0, 3.5, 5.0)
    for index in range(3):
        add_curve(
            channelbag,
            f"{root_path}.location",
            index,
            [
                (frame, offset + frame * (index + 1) * 0.1)
                for frame in frames
            ],
        )

    rotations = [
        Euler((0.10 + offset, -0.05, 0.15), 'XYZ'),
        Euler((0.35 + offset, 0.20, -0.10), 'XYZ'),
        Euler((0.50 + offset, -0.25, 0.30), 'XYZ'),
    ]
    if rotation_mode == 'QUATERNION':
        quaternion_values = [tuple(rotation.to_quaternion()) for rotation in rotations]
        for index in range(4):
            add_curve(
                channelbag,
                f"{root_path}.rotation_quaternion",
                index,
                [
                    (frame, quaternion_values[key_index][index])
                    for key_index, frame in enumerate(frames)
                ],
            )
    elif rotation_mode == 'AXIS_ANGLE':
        axis_angle_values = []
        for rotation in rotations:
            axis, angle = rotation.to_quaternion().to_axis_angle()
            axis_angle_values.append((angle, axis.x, axis.y, axis.z))
        for index in range(4):
            add_curve(
                channelbag,
                f"{root_path}.rotation_axis_angle",
                index,
                [
                    (frame, axis_angle_values[key_index][index])
                    for key_index, frame in enumerate(frames)
                ],
            )
    else:
        for index in range(3):
            add_curve(
                channelbag,
                f"{root_path}.rotation_euler",
                index,
                [
                    (frame, rotations[key_index][index])
                    for key_index, frame in enumerate(frames)
                ],
            )

    child_path = f'pose.bones["{root_name}_Child"].location'
    child_curve = add_curve(
        channelbag,
        child_path,
        0,
        [(1.0, 0.0), (3.5, 0.25 + offset), (5.0, 0.5 + offset)],
    )
    return action, slot, child_curve


def curve_coordinates(fcurve):
    return [(float(point.co[0]), float(point.co[1])) for point in fcurve.keyframe_points]


def run_success_case(addon, rotation_mode, suffix):
    root_name = f"Root{suffix}"
    obj = create_rig(f"Rig{suffix}", root_name)
    obj.pose.bones[root_name].rotation_mode = rotation_mode

    action_specs = (
        (f"Walk{suffix}", 0.0, False),
        (f"Run{suffix}", 0.2, True),
        (f"Jump{suffix}", -0.15, False),
    )
    actions = []
    child_curve_coordinates = {}
    slots = {}
    for action_name, offset, cyclic in action_specs:
        action, slot, child_curve = create_action(
            obj, root_name, action_name, rotation_mode, offset, cyclic
        )
        actions.append(action)
        slots[action.name] = slot
        child_curve_coordinates[action.name] = curve_coordinates(child_curve)

    animation_data = obj.animation_data_create()
    animation_data.action = actions[0]
    animation_data.action_slot = slots[actions[0].name]
    nla_track = animation_data.nla_tracks.new()
    nla_track.name = "Root Reorient Test"
    nla_strip = nla_track.strips.new(actions[2].name, 1, actions[2])
    nla_strip.action_slot = slots[actions[2].name]

    bone_path = addon._bone_data_path(root_name)
    old_rest = obj.data.bones[root_name].matrix_local.copy()
    child_rest = obj.data.bones[f"{root_name}_Child"].matrix_local.copy()
    targets = addon._find_matching_targets(bone_path)
    assert len(targets) == 3

    old_bases = {}
    target_frames = {}
    for target in targets:
        frames = addon._target_frames(target, addon._transform_paths(bone_path))
        target_frames[target.action.name] = frames
        curves = addon._fcurve_map(target)
        old_bases[target.action.name] = {
            frame: addon._sample_basis(curves, bone_path, rotation_mode, frame)
            for frame in frames
        }

    settings = bpy.context.scene.root_bone_reorient
    settings.target_tail = (0.0, 1.0, 0.0)
    settings.target_roll = 0.0
    settings.create_backups = True

    result = bpy.ops.anim.root_bone_reorient_preserve_motion()
    assert result == {'FINISHED'}
    assert animation_data.action == actions[0]
    assert nla_strip.action == actions[2]

    new_rest = obj.data.bones[root_name].matrix_local.copy()
    assert Vector(obj.data.bones[root_name].tail_local) == Vector((0.0, 1.0, 0.0))
    assert_matrix_close(new_rest, Matrix.Identity(4))

    new_targets = {
        target.action.name: target
        for target in addon._find_matching_targets(bone_path)
        if target.action in actions
    }
    assert set(new_targets) == {action.name for action in actions}

    child_path = f'pose.bones["{root_name}_Child"].location'
    for action in actions:
        target = new_targets[action.name]
        curves = addon._fcurve_map(target)
        new_child_curve = curves[(child_path, 0)]
        assert curve_coordinates(new_child_curve) == child_curve_coordinates[action.name]

        expected_frames = {1.0, 2.0, 3.0, 3.5, 4.0, 5.0}
        assert set(target_frames[action.name]) == expected_frames
        for frame in target_frames[action.name]:
            old_basis = old_bases[action.name][frame]
            new_basis = addon._sample_basis(curves, bone_path, rotation_mode, frame)

            old_pose = old_rest @ old_basis
            new_pose = new_rest @ new_basis
            assert_matrix_close(
                new_pose @ new_rest.inverted(),
                old_pose @ old_rest.inverted(),
            )
            assert (new_pose.translation - old_pose.translation).length <= 2.0e-5

            child_location = new_child_curve.evaluate(frame)
            child_basis = Matrix.Translation((child_location, 0.0, 0.0))
            old_child_pose = (
                old_pose @ old_rest.inverted() @ child_rest @ child_basis
            )
            new_child_pose = (
                new_pose @ new_rest.inverted() @ child_rest @ child_basis
            )
            assert_matrix_close(new_child_pose, old_child_pose)

        root_curves = [
            curve
            for curve in target.fcurves
            if curve.data_path in addon._transform_paths(bone_path)
        ]
        expected_curve_count = 10 if rotation_mode in {'QUATERNION', 'AXIS_ANGLE'} else 9
        assert len(root_curves) == expected_curve_count
        assert all(len(curve.keyframe_points) == 6 for curve in root_curves)
        assert all(
            point.interpolation == 'LINEAR'
            for curve in root_curves
            for point in curve.keyframe_points
        )
        if action.use_cyclic:
            assert all(
                any(modifier.type == 'CYCLES' for modifier in curve.modifiers)
                for curve in root_curves
            )

    backup_sources = {
        action.get(addon._BACKUP_SOURCE)
        for action in bpy.data.actions
        if action.get(addon._BACKUP_FLAG, False)
    }
    assert {action.name for action in actions}.issubset(backup_sources)


def run_failed_validation_case(addon):
    root_name = "RootInvalid"
    obj = create_rig("RigInvalid", root_name, connected_child=True)
    obj.pose.bones[root_name].rotation_mode = 'XYZ'
    action, _slot, root_curve = create_action(
        obj, root_name, "InvalidConnectedAction", 'XYZ', 0.0
    )
    old_curve = curve_coordinates(root_curve)
    old_tail = obj.data.bones[root_name].tail_local.copy()

    settings = bpy.context.scene.root_bone_reorient
    settings.target_tail = (0.0, 1.0, 0.0)
    settings.target_roll = 0.0
    try:
        result = bpy.ops.anim.root_bone_reorient_preserve_motion()
    except RuntimeError as error:
        assert "connected children" in str(error)
        result = {'CANCELLED'}
    assert result == {'CANCELLED'}
    assert obj.data.bones[root_name].tail_local == old_tail
    assert curve_coordinates(root_curve) == old_curve
    assert not any(
        candidate.get(addon._BACKUP_SOURCE) == action.name
        for candidate in bpy.data.actions
        if candidate.get(addon._BACKUP_FLAG, False)
    )


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    addon = load_addon()
    run_success_case(addon, 'XYZ', "Euler")
    run_success_case(addon, 'QUATERNION', "Quaternion")
    run_success_case(addon, 'AXIS_ANGLE', "AxisAngle")
    run_failed_validation_case(addon)
    print("ROOT_BONE_REORIENT_TESTS_PASSED")


if __name__ == "__main__":
    main()
