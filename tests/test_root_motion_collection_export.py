"""Run with Blender 4.5+:

blender --background --factory-startup --python tests/test_root_motion_collection_export.py
"""

import importlib.util
import math
from pathlib import Path

import bpy
from mathutils import Euler


ADDON_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "root_motion_collection_export.py"
)


def load_addon():
    spec = importlib.util.spec_from_file_location(
        "root_motion_collection_export_tested",
        ADDON_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register()
    return module


def assert_matrix_close(actual, expected, tolerance=3.0e-4):
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
        point.interpolation = 'LINEAR'
    curve.update()
    return curve


def create_scene():
    collection = bpy.data.collections.new("RootMotionExportTest")
    bpy.context.scene.collection.children.link(collection)

    armature = bpy.data.armatures.new("SourceArmatureData")
    source = bpy.data.objects.new("SourceRig", armature)
    collection.objects.link(source)
    bpy.context.view_layer.objects.active = source
    source.select_set(True)

    bpy.ops.object.mode_set(mode='EDIT')
    root = armature.edit_bones.new("Root Motion")
    root.head = (0.25, -0.15, 0.1)
    root.tail = (0.25, 0.85, 0.1)

    child = armature.edit_bones.new("Hips")
    child.head = (0.25, 0.85, 0.1)
    child.tail = (0.25, 1.85, 0.1)
    child.parent = root
    child.use_connect = False
    bpy.ops.object.mode_set(mode='OBJECT')

    action = bpy.data.actions.new("Walk")
    slot = action.slots.new('OBJECT', source.name)
    layer = action.layers.new("Animation")
    strip = layer.strips.new(type='KEYFRAME')
    channelbag = strip.channelbags.new(slot)
    action.use_frame_range = True
    action.frame_start = 1.0
    action.frame_end = 5.0

    root_path = 'pose.bones["Root Motion"]'
    for index in range(3):
        add_curve(
            channelbag,
            f"{root_path}.location",
            index,
            [(1.0, 0.0), (3.0, (index + 1) * 0.4), (5.0, (index + 1) * 0.8)],
        )

    root_rotations = (
        Euler((0.0, 0.0, 0.0)).to_quaternion(),
        Euler((0.1, -0.2, 0.35)).to_quaternion(),
        Euler((0.2, -0.1, 0.7)).to_quaternion(),
    )
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
    source.pose.bones["Root Motion"].rotation_mode = 'QUATERNION'

    child_path = 'pose.bones["Hips"].location'
    add_curve(
        channelbag,
        child_path,
        2,
        [(1.0, 0.0), (3.0, 0.2), (5.0, 0.0)],
    )

    for index, keys in enumerate(
        (
            [(1.0, 1.0), (3.0, 2.0), (5.0, 3.0)],
            [(1.0, -2.0), (3.0, -1.0), (5.0, 0.0)],
            [(1.0, 0.5), (3.0, 0.5), (5.0, 0.5)],
        )
    ):
        add_curve(channelbag, "location", index, keys)

    animation_data = source.animation_data_create()
    animation_data.action = action
    animation_data.action_slot = slot

    mesh = bpy.data.meshes.new("MeshData")
    mesh.from_pydata(
        [(-0.5, 0.0, 0.0), (0.5, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [],
        [(0, 1, 2)],
    )
    skinned = bpy.data.objects.new("SkinnedMesh", mesh)
    collection.objects.link(skinned)
    skinned.parent = source
    modifier = skinned.modifiers.new("Armature", 'ARMATURE')
    modifier.object = source
    return collection, source, skinned, modifier, action


def expected_child_world_matrices(scene, source, frames):
    expected = {}
    for frame in frames:
        scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
        expected[frame] = (
            evaluated.matrix_world
            @ evaluated.pose.bones["Hips"].matrix
        )
    return expected


def run_success_case(addon):
    collection, source, skinned, modifier, action = create_scene()
    expected = expected_child_world_matrices(bpy.context.scene, source, range(1, 6))
    source_pointer = source.as_pointer()
    action_pointer = action.as_pointer()
    original_frame = 4
    bpy.context.scene.frame_set(original_frame)

    callback_seen = {"value": False}

    def inspect_temporary(context, target_collection, temp, frames):
        callback_seen["value"] = True
        assert target_collection == collection
        assert temp.name == "Root Motion"
        assert temp.data.bones.get("Root Motion") is None
        assert temp.data.bones.get("Hips") is not None
        assert source not in collection.objects[:]
        assert temp in collection.objects[:]
        assert skinned.parent == temp
        assert modifier.object == temp
        assert list(frames) == [1.0, 2.0, 3.0, 4.0, 5.0]

        for frame in frames:
            context.scene.frame_set(int(frame))
            context.view_layer.update()
            evaluated = temp.evaluated_get(context.evaluated_depsgraph_get())
            actual = evaluated.matrix_world @ evaluated.pose.bones["Hips"].matrix
            assert_matrix_close(actual, expected[frame])

    summary = addon.run_root_motion_export(
        bpy.context,
        source,
        "Root Motion",
        collection,
        export_callback=inspect_temporary,
    )
    assert callback_seen["value"]
    assert summary["object_name"] == "Root Motion"
    assert summary["frame_count"] == 5

    assert source.as_pointer() == source_pointer
    assert source in collection.objects[:]
    assert skinned.parent == source
    assert modifier.object == source
    assert action.as_pointer() == action_pointer
    assert source.animation_data.action == action
    assert bpy.context.scene.frame_current == original_frame
    assert bpy.data.objects.get("Root Motion") is None
    assert not any(
        candidate.get(addon._TEMP_ACTION_TAG, False)
        for candidate in bpy.data.actions
    )


def run_failure_cleanup_case(addon):
    collection = bpy.data.collections["RootMotionExportTest"]
    source = bpy.data.objects["SourceRig"]
    skinned = bpy.data.objects["SkinnedMesh"]
    modifier = skinned.modifiers["Armature"]

    def fail_export(_context, _collection, _temp, _frames):
        raise RuntimeError("intentional exporter failure")

    try:
        addon.run_root_motion_export(
            bpy.context,
            source,
            "Root Motion",
            collection,
            export_callback=fail_export,
        )
    except RuntimeError as error:
        assert "intentional exporter failure" in str(error)
    else:
        raise AssertionError("Expected the simulated exporter failure")

    assert source in collection.objects[:]
    assert skinned.parent == source
    assert modifier.object == source
    assert bpy.data.objects.get("Root Motion") is None
    assert not any(
        candidate.get(addon._TEMP_ACTION_TAG, False)
        for candidate in bpy.data.actions
    )


def run_operator_validation_case(addon):
    collection = bpy.data.collections["RootMotionExportTest"]
    source = bpy.data.objects["SourceRig"]
    bpy.context.view_layer.objects.active = source
    source.select_set(True)
    result = bpy.ops.export_scene.root_motion_collection(
        root_bone="Root Motion",
        target_collection=collection.name,
        range_mode='ACTION',
        sample_step=1.0,
        dry_run=True,
    )
    assert result == {'FINISHED'}
    assert source in collection.objects[:]
    assert bpy.data.objects.get("Root Motion") is None


def run_saved_fbx_export_case(addon):
    collection = bpy.data.collections["RootMotionExportTest"]
    source = bpy.data.objects["SourceRig"]
    output_path = Path(__file__).resolve().parent / "_root_motion_collection_export_test.fbx"
    if output_path.exists():
        output_path.unlink()

    exporter = collection.exporters.new('IO_FH_fbx', name="Automated FBX Test")
    exporter.filepath = str(output_path)
    exporter.export_properties.filepath = str(output_path)
    exporter.export_properties.use_active_collection = True
    try:
        summary = addon.run_root_motion_export(
            bpy.context,
            source,
            "Root Motion",
            collection,
        )
        assert summary["dry_run"] is False
        assert output_path.exists()
        assert output_path.stat().st_size > 0
        assert source in collection.objects[:]
        assert bpy.data.objects.get("Root Motion") is None
    finally:
        collection.exporters.remove(exporter)
        if output_path.exists():
            output_path.unlink()


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    addon = load_addon()
    run_success_case(addon)
    run_failure_cleanup_case(addon)
    run_operator_validation_case(addon)
    run_saved_fbx_export_case(addon)
    print("ROOT_MOTION_COLLECTION_EXPORT_TESTS_PASSED")


if __name__ == "__main__":
    main()
