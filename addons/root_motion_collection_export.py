# SPDX-License-Identifier: GPL-2.0-or-later

"""Export a temporary root-motion version of the active armature.

The source armature is never modified.  A temporary copy replaces it in one
of its collections for the duration of the collection's saved export:

1. The selected parentless root bone's evaluated motion is baked onto the
   temporary armature object.
2. The root bone and its transform F-curves are removed from the copy.
3. The copy is named after the removed root bone.
4. The collection's configured exporters are invoked.
5. All temporary links and data are restored/removed, including on failure.
"""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty
from bpy.types import Operator, Panel
from mathutils import Matrix


bl_info = {
    "name": "Root Motion Collection Export",
    "author": "OpenAI",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Animation > Root Motion Export",
    "description": (
        "Temporarily move root-bone animation to an armature object and run "
        "the collection's saved exporters"
    ),
    "category": "Import-Export",
}


_MAX_SAMPLES = 250_000
_MATRIX_TOLERANCE = 2.0e-4
_TEMP_ACTION_TAG = "_root_motion_export_temporary"
_OBJECT_TRANSFORM_PATHS = {
    "location",
    "rotation_euler",
    "rotation_quaternion",
    "rotation_axis_angle",
    "scale",
    "delta_location",
    "delta_rotation_euler",
    "delta_rotation_quaternion",
    "delta_scale",
}
_BONE_TRANSFORM_PROPERTIES = {
    "location",
    "rotation_euler",
    "rotation_quaternion",
    "rotation_axis_angle",
    "scale",
}


class RootMotionExportError(RuntimeError):
    pass


def _bone_path(bone_name):
    escaped = bpy.utils.escape_identifier(bone_name)
    return f'pose.bones["{escaped}"]'


def _root_transform_paths(bone_name):
    base = _bone_path(bone_name)
    return {f"{base}.{prop}" for prop in _BONE_TRANSFORM_PROPERTIES}


def _active_bone_name(obj):
    if obj is None or obj.type != 'ARMATURE':
        return ""
    if obj.mode == 'EDIT':
        bone = obj.data.edit_bones.active
    else:
        bone = obj.data.bones.active
    return bone.name if bone is not None else ""


def _root_bone_items(_operator, context):
    obj = context.active_object
    if obj is None or obj.type != 'ARMATURE':
        return [("__NONE__", "No active armature", "Select an armature")]

    roots = [bone for bone in obj.data.bones if bone.parent is None]
    if not roots:
        return [("__NONE__", "No parentless bones", "The armature has no root bone")]
    return [
        (bone.name, bone.name, "Move this bone's animation to the object", 'BONE_DATA', index)
        for index, bone in enumerate(roots)
    ]


def _collection_items(_operator, context):
    obj = context.active_object
    if obj is None:
        return [("__NONE__", "No active object", "Select an armature")]

    collections = list(obj.users_collection)
    if not collections:
        return [("__NONE__", "Object is not in a collection", "")]
    return [
        (
            collection.name,
            collection.name,
            "Use this collection's saved exporters",
            'OUTLINER_COLLECTION',
            index,
        )
        for index, collection in enumerate(collections)
    ]


def _find_layer_collection(layer_collection, collection):
    if layer_collection.collection == collection:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, collection)
        if found is not None:
            return found
    return None


def _action_slot_copy(source_action, copied_action, source_slot):
    if source_slot is None:
        return None

    source_identifier = getattr(source_slot, "identifier", "")
    for index, slot in enumerate(copied_action.slots):
        if getattr(slot, "identifier", "") == source_identifier:
            return slot
        if index < len(source_action.slots) and source_action.slots[index] == source_slot:
            return copied_action.slots[index]
    return None


def _channelbags_for_slot(action, slot):
    if not getattr(action, "is_action_layered", False):
        fcurves = getattr(action, "fcurves", None)
        if fcurves is None:
            raise RootMotionExportError(
                f'Action "{action.name}" does not expose editable F-curves'
            )
        return [(fcurves, getattr(action, "groups", None), False)]

    bags = []
    for layer in action.layers:
        for strip in layer.strips:
            channelbags = getattr(strip, "channelbags", None)
            if channelbags is None:
                continue
            for bag in channelbags:
                if slot is None or getattr(bag, "slot", None) == slot:
                    bags.append((bag.fcurves, bag.groups, True))

    if not bags:
        raise RootMotionExportError(
            f'No editable channel bag was found in action "{action.name}"'
        )
    return bags


def _curve_key_frames(action, slot, relevant_paths):
    frames = set()
    for fcurves, _groups, _is_layered in _channelbags_for_slot(action, slot):
        for curve in fcurves:
            if curve.data_path not in relevant_paths:
                continue
            for point in curve.keyframe_points:
                frame = float(point.co[0])
                if math.isfinite(frame):
                    frames.add(frame)
            for point in curve.sampled_points:
                frame = float(point.co[0])
                if math.isfinite(frame):
                    frames.add(frame)
    return frames


def _animation_range(scene, action, range_mode):
    if range_mode == 'SCENE':
        return float(scene.frame_start), float(scene.frame_end)

    if getattr(action, "use_frame_range", False):
        start = float(action.frame_start)
        end = float(action.frame_end)
    else:
        action_range = action.curve_frame_range
        start = float(action_range[0])
        end = float(action_range[1])

    if not math.isfinite(start) or not math.isfinite(end) or start == end:
        start = float(scene.frame_start)
        end = float(scene.frame_end)
    if end < start:
        start, end = end, start
    return start, end


def _sample_frames(scene, action, slot, bone_name, range_mode, step):
    if not math.isfinite(step) or step <= 0.0:
        raise RootMotionExportError("Sample step must be greater than zero")

    start, end = _animation_range(scene, action, range_mode)
    if not math.isfinite(start) or not math.isfinite(end):
        raise RootMotionExportError("Animation frame range is not finite")

    span = max(0.0, end - start)
    regular_count = int(math.floor(span / step)) + 1
    if regular_count > _MAX_SAMPLES:
        raise RootMotionExportError(
            f"The requested range needs {regular_count:,} samples; "
            f"the safety limit is {_MAX_SAMPLES:,}"
        )

    frames = {start, end}
    frames.update(start + index * step for index in range(regular_count))
    relevant_paths = _root_transform_paths(bone_name) | _OBJECT_TRANSFORM_PATHS
    frames.update(
        frame
        for frame in _curve_key_frames(action, slot, relevant_paths)
        if start <= frame <= end
    )
    frames = sorted(frames)

    if len(frames) > _MAX_SAMPLES:
        raise RootMotionExportError(
            f"The requested range needs {len(frames):,} samples; "
            f"the safety limit is {_MAX_SAMPLES:,}"
        )
    return frames


def _set_scene_frame(scene, frame):
    integer_frame = math.floor(frame)
    scene.frame_set(integer_frame, subframe=frame - integer_frame)


def _matrix_error(left, right):
    return max(
        abs(float(left[row][column]) - float(right[row][column]))
        for row in range(4)
        for column in range(4)
    )


def _decompose_matrix(matrix, frame):
    location, rotation, scale = matrix.decompose()
    rotation.normalize()
    recomposed = Matrix.LocRotScale(location, rotation, scale)
    error = _matrix_error(matrix, recomposed)
    if error > _MATRIX_TOLERANCE:
        raise RootMotionExportError(
            f"Root motion contains unsupported shear at frame {frame:g} "
            f"(matrix error {error:.3g})"
        )
    return location, rotation, scale


def _bake_root_motion(context, source, bone_name, frames):
    scene = context.scene
    root_rest = source.data.bones[bone_name].matrix_local.copy()
    root_rest_inverse = root_rest.inverted()
    samples = []
    previous_rotation = None

    for frame in frames:
        _set_scene_frame(scene, frame)
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = source.evaluated_get(depsgraph)
        pose_bone = evaluated.pose.bones.get(bone_name)
        if pose_bone is None:
            raise RootMotionExportError(
                f'Pose bone "{bone_name}" disappeared while sampling'
            )

        root_delta = pose_bone.matrix @ root_rest_inverse
        desired_world = evaluated.matrix_world @ root_delta
        location, rotation, scale = _decompose_matrix(desired_world, frame)

        if previous_rotation is not None and rotation.dot(previous_rotation) < 0.0:
            rotation.negate()
        previous_rotation = rotation.copy()
        samples.append(
            (
                tuple(float(value) for value in location),
                tuple(float(value) for value in rotation),
                tuple(float(value) for value in scale),
            )
        )
    return samples


def _ensure_group(groups, name):
    if groups is None:
        return None
    group = groups.get(name)
    if group is None:
        group = groups.new(name)
    return group


def _new_fcurve(fcurves, groups, is_layered, data_path, index, group_name):
    if is_layered:
        curve = fcurves.new(data_path, index=index)
        group = _ensure_group(groups, group_name)
        if group is not None:
            curve.group = group
        return curve
    return fcurves.new(
        data_path,
        index=index,
        action_group=group_name,
    )


def _write_motion_curves(action, slot, bone_name, frames, samples):
    bags = _channelbags_for_slot(action, slot)
    remove_paths = _root_transform_paths(bone_name) | _OBJECT_TRANSFORM_PATHS
    for fcurves, _groups, _is_layered in bags:
        for curve in list(fcurves):
            if curve.data_path in remove_paths:
                fcurves.remove(curve)

    target_fcurves, target_groups, is_layered = bags[0]
    channels = {
        "location": [[], [], []],
        "rotation_quaternion": [[], [], [], []],
        "scale": [[], [], []],
    }
    for location, rotation, scale in samples:
        for index, value in enumerate(location):
            channels["location"][index].append(value)
        for index, value in enumerate(rotation):
            channels["rotation_quaternion"][index].append(value)
        for index, value in enumerate(scale):
            channels["scale"][index].append(value)

    for data_path, components in channels.items():
        for array_index, values in enumerate(components):
            curve = _new_fcurve(
                target_fcurves,
                target_groups,
                is_layered,
                data_path,
                array_index,
                "Object Transforms",
            )
            curve.extrapolation = 'CONSTANT'
            curve.keyframe_points.add(count=len(frames))
            coordinates = []
            for frame, value in zip(frames, values):
                coordinates.extend((frame, value))
            curve.keyframe_points.foreach_set("co", coordinates)
            for point in curve.keyframe_points:
                point.interpolation = 'LINEAR'
                point.handle_left_type = 'VECTOR'
                point.handle_right_type = 'VECTOR'
            curve.update()
    action.update_tag()


def _remove_root_bone(context, obj, bone_name):
    old_active = context.view_layer.objects.active
    old_mode = obj.mode
    try:
        context.view_layer.objects.active = obj
        obj.select_set(True)
        if obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')

        root = obj.data.edit_bones.get(bone_name)
        if root is None:
            raise RootMotionExportError(f'Edit bone "{bone_name}" is unavailable')

        child_matrices = {
            child.name: child.matrix.copy()
            for child in root.children
        }
        for child in list(root.children):
            child.use_connect = False
            child.parent = None
            child.matrix = child_matrices[child.name]
        obj.data.edit_bones.remove(root)
    finally:
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if old_active is not None and old_active.name in context.view_layer.objects:
            context.view_layer.objects.active = old_active
        elif obj.name in context.view_layer.objects:
            context.view_layer.objects.active = obj
        if old_mode not in {'OBJECT', 'EDIT'} and obj.name in context.view_layer.objects:
            try:
                bpy.ops.object.mode_set(mode=old_mode)
            except RuntimeError:
                pass


def _validate(context, source, bone_name, collection, range_mode, sample_step):
    if source is None or source.type != 'ARMATURE':
        raise RootMotionExportError("The active object must be an armature")
    if source.library is not None or source.data.library is not None:
        raise RootMotionExportError("Linked armatures are read-only and cannot be copied safely")
    if collection is None or collection not in source.users_collection:
        raise RootMotionExportError(
            "Choose a collection that directly contains the active armature"
        )

    bone = source.data.bones.get(bone_name)
    if bone is None:
        raise RootMotionExportError(f'Bone "{bone_name}" does not exist')
    if bone.parent is not None:
        raise RootMotionExportError(
            f'Bone "{bone_name}" is not parentless; choose the actual root bone'
        )

    animation_data = source.animation_data
    action = animation_data.action if animation_data is not None else None
    if action is None:
        raise RootMotionExportError(
            "The armature needs an active Action (stashed NLA-only animation is not enough)"
        )

    root_path_prefix = _bone_path(bone_name)
    dependency_descriptions = []
    for pose_bone in source.pose.bones:
        if pose_bone.name == bone_name:
            continue
        for constraint in pose_bone.constraints:
            for property_name in ("subtarget", "pole_subtarget"):
                if getattr(constraint, property_name, "") == bone_name:
                    dependency_descriptions.append(
                        f'{pose_bone.name}: constraint "{constraint.name}"'
                    )
            targets = getattr(constraint, "targets", None)
            if targets is not None:
                for target in targets:
                    if getattr(target, "subtarget", "") == bone_name:
                        dependency_descriptions.append(
                            f'{pose_bone.name}: constraint "{constraint.name}"'
                        )
    if dependency_descriptions:
        preview = ", ".join(dependency_descriptions[:4])
        raise RootMotionExportError(
            f'Other bones depend on "{bone_name}" ({preview}); bake or remove those '
            "constraints before exporting"
        )

    if animation_data is not None:
        for driver in animation_data.drivers:
            if driver.data_path.startswith(root_path_prefix):
                raise RootMotionExportError(
                    "The chosen root transform has drivers; bake them to the active Action first"
                )

    slot = getattr(animation_data, "action_slot", None)
    frames = _sample_frames(
        context.scene,
        action,
        slot,
        bone_name,
        range_mode,
        sample_step,
    )
    return action, slot, frames


def _copy_armature_with_action(source, source_action, source_slot, collection):
    temp = source.copy()
    temp.data = source.data.copy()
    copied_armature = temp.data
    copied_action = None
    try:
        collection.objects.link(temp)

        temp.animation_data_clear()
        animation_data = temp.animation_data_create()
        copied_action = source_action.copy()
        copied_action.name = f"{source_action.name}__root_motion_export"
        copied_action[_TEMP_ACTION_TAG] = True
        animation_data.action = copied_action

        copied_slot = _action_slot_copy(source_action, copied_action, source_slot)
        if copied_slot is not None:
            animation_data.action_slot = copied_slot

        temp.parent = None
        temp.parent_type = 'OBJECT'
        temp.parent_bone = ""
        temp.matrix_parent_inverse.identity()
        temp.constraints.clear()
        temp.delta_location = (0.0, 0.0, 0.0)
        temp.delta_rotation_euler = (0.0, 0.0, 0.0)
        temp.delta_rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        temp.delta_scale = (1.0, 1.0, 1.0)
        temp.rotation_mode = 'QUATERNION'
        return temp, copied_action, copied_slot
    except Exception:
        bpy.data.objects.remove(temp, do_unlink=True)
        if copied_action is not None and copied_action.users == 0:
            bpy.data.actions.remove(copied_action)
        if copied_armature.users == 0:
            bpy.data.armatures.remove(copied_armature)
        raise


def _objects_in_collection_tree(collection):
    return list(collection.all_objects)


def _retarget_collection_references(collection, source, temp, removed_bone_name):
    parent_changes = []
    modifier_changes = []
    constraint_changes = []
    objects = _objects_in_collection_tree(collection)

    for obj in objects:
        if (
            obj != source
            and obj != temp
            and obj.parent == source
            and obj.parent_type == 'BONE'
            and obj.parent_bone == removed_bone_name
        ):
            raise RootMotionExportError(
                f'Object "{obj.name}" is parented to the root bone that will be removed'
            )

    try:
        for obj in objects:
            if obj == source or obj == temp:
                continue

            if obj.parent == source:
                parent_changes.append(
                    (
                        obj,
                        obj.parent,
                        obj.parent_type,
                        obj.parent_bone,
                        obj.matrix_parent_inverse.copy(),
                    )
                )
                obj.parent = temp

            for modifier in obj.modifiers:
                if getattr(modifier, "object", None) == source:
                    modifier_changes.append((modifier, source))
                    modifier.object = temp

            for constraint in obj.constraints:
                if getattr(constraint, "target", None) == source:
                    constraint_changes.append((constraint, source))
                    constraint.target = temp
    except Exception:
        _restore_collection_references(
            (parent_changes, modifier_changes, constraint_changes)
        )
        raise

    return parent_changes, modifier_changes, constraint_changes


def _restore_collection_references(changes):
    parent_changes, modifier_changes, constraint_changes = changes
    for constraint, target in reversed(constraint_changes):
        if constraint.id_data is not None:
            constraint.target = target
    for modifier, target in reversed(modifier_changes):
        if modifier.id_data is not None:
            modifier.object = target
    for obj, parent, parent_type, parent_bone, parent_inverse in reversed(parent_changes):
        if obj.name not in bpy.data.objects:
            continue
        obj.parent = parent
        obj.parent_type = parent_type
        obj.parent_bone = parent_bone
        obj.matrix_parent_inverse = parent_inverse


def _export_saved_collection(context, collection, temp):
    if not hasattr(collection, "exporters"):
        raise RootMotionExportError(
            "Saved collection exporters require Blender 4.1 or newer"
        )
    if len(collection.exporters) == 0:
        raise RootMotionExportError(
            f'Collection "{collection.name}" has no saved exporters'
        )

    override = {
        "collection": collection,
        "active_object": temp,
        "object": temp,
        "selected_objects": [temp],
        "selected_editable_objects": [temp],
    }
    layer_collection = _find_layer_collection(
        context.view_layer.layer_collection,
        collection,
    )
    if layer_collection is not None:
        override["layer_collection"] = layer_collection

    with context.temp_override(**override):
        if not bpy.ops.collection.export_all.poll():
            raise RootMotionExportError(
                f'Blender cannot invoke the saved exporters for "{collection.name}" '
                "in the current context"
            )
        result = bpy.ops.collection.export_all()
    if 'FINISHED' not in result:
        raise RootMotionExportError(
            f'Collection export did not finish (result: {sorted(result)})'
        )
    return result


def _capture_selection(context):
    return (
        context.view_layer.objects.active,
        list(context.selected_objects),
        context.active_object.mode if context.active_object is not None else 'OBJECT',
    )


def _restore_selection(context, state):
    active, selected, mode = state
    if context.object is not None and context.object.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass

    for obj in context.view_layer.objects:
        obj.select_set(False)
    for obj in selected:
        if obj.name in context.view_layer.objects:
            obj.select_set(True)
    if active is not None and active.name in context.view_layer.objects:
        context.view_layer.objects.active = active
        if mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode=mode)
            except RuntimeError:
                pass


def run_root_motion_export(
    context,
    source,
    bone_name,
    collection,
    *,
    range_mode='ACTION',
    sample_step=1.0,
    dry_run=False,
    export_callback=None,
):
    """Run the reversible conversion/export transaction.

    ``export_callback`` is an optional test hook accepting
    ``(context, collection, temporary_object, frames)``.
    """

    source_action, source_slot, frames = _validate(
        context,
        source,
        bone_name,
        collection,
        range_mode,
        sample_step,
    )
    if not dry_run and export_callback is None:
        if not hasattr(collection, "exporters") or len(collection.exporters) == 0:
            raise RootMotionExportError(
                f'Collection "{collection.name}" has no saved exporters'
            )

    selection_state = _capture_selection(context)
    old_frame = float(context.scene.frame_current_final)
    old_use_nla = (
        bool(source.animation_data.use_nla)
        if source.animation_data is not None
        and hasattr(source.animation_data, "use_nla")
        else None
    )
    temp = None
    copied_action = None
    copied_armature = None
    reference_changes = ([], [], [])
    source_was_linked = source.name in collection.objects
    collision = bpy.data.objects.get(bone_name)
    collision_original_name = collision.name if collision is not None else None
    exported_name = bone_name

    try:
        if context.object is not None and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if old_use_nla is not None:
            source.animation_data.use_nla = False

        samples = _bake_root_motion(context, source, bone_name, frames)
        temp, copied_action, copied_slot = _copy_armature_with_action(
            source,
            source_action,
            source_slot,
            collection,
        )
        copied_armature = temp.data
        _write_motion_curves(
            copied_action,
            copied_slot,
            bone_name,
            frames,
            samples,
        )
        _remove_root_bone(context, temp, bone_name)

        reference_changes = _retarget_collection_references(
            collection,
            source,
            temp,
            bone_name,
        )
        if source_was_linked:
            collection.objects.unlink(source)

        if collision is not None and collision != temp:
            collision.name = f"{collision_original_name}__root_motion_export_source"
        temp.name = bone_name
        temp.data.name = f"{bone_name}_Armature"
        if temp.name != bone_name:
            raise RootMotionExportError(
                f'Could not reserve the exact temporary object name "{bone_name}"'
            )

        context.scene.frame_set(math.floor(frames[0]), subframe=frames[0] - math.floor(frames[0]))
        context.view_layer.update()

        if export_callback is not None:
            export_callback(context, collection, temp, frames)
        elif not dry_run:
            _export_saved_collection(context, collection, temp)
        return {
            "object_name": exported_name,
            "action_name": source_action.name,
            "frame_count": len(frames),
            "frame_start": frames[0],
            "frame_end": frames[-1],
            "dry_run": bool(dry_run),
        }
    finally:
        try:
            _restore_collection_references(reference_changes)
        finally:
            if source_was_linked and source.name not in collection.objects:
                collection.objects.link(source)

            if temp is not None and temp.name in bpy.data.objects:
                bpy.data.objects.remove(temp, do_unlink=True)
            if copied_action is not None and copied_action.name in bpy.data.actions:
                if copied_action.users == 0:
                    bpy.data.actions.remove(copied_action)
            if copied_armature is not None and copied_armature.name in bpy.data.armatures:
                if copied_armature.users == 0:
                    bpy.data.armatures.remove(copied_armature)

            if collision is not None and collision.name in bpy.data.objects:
                collision.name = collision_original_name
            if old_use_nla is not None and source.animation_data is not None:
                source.animation_data.use_nla = old_use_nla

            _set_scene_frame(context.scene, old_frame)
            context.view_layer.update()
            _restore_selection(context, selection_state)


class ROOTMOTIONEXPORT_OT_export(Operator):
    bl_idname = "export_scene.root_motion_collection"
    bl_label = "Export Root Motion Copy"
    bl_description = (
        "Bake root-bone motion to a temporary armature object, remove the root "
        "bone, run the collection's saved exporters, then clean up"
    )
    bl_options = {'REGISTER'}

    root_bone: EnumProperty(
        name="Root Bone",
        description="Parentless bone whose animation will become object animation",
        items=_root_bone_items,
    )
    target_collection: EnumProperty(
        name="Collection",
        description="Collection whose saved exporters will be invoked",
        items=_collection_items,
    )
    range_mode: EnumProperty(
        name="Frame Range",
        items=(
            ('ACTION', "Active Action", "Use the active Action's frame range"),
            ('SCENE', "Scene", "Use the scene start/end frame range"),
        ),
        default='ACTION',
    )
    sample_step: FloatProperty(
        name="Sample Step",
        description="Bake interval in frames; smaller values create more keys",
        default=1.0,
        min=0.01,
        soft_max=2.0,
    )
    dry_run: BoolProperty(
        name="Validate Only",
        description="Build and clean the temporary rig without writing export files",
        default=False,
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE'

    def invoke(self, context, _event):
        obj = context.active_object
        active_bone = _active_bone_name(obj)
        if active_bone and obj.data.bones[active_bone].parent is None:
            self.root_bone = active_bone
        else:
            roots = [bone for bone in obj.data.bones if bone.parent is None]
            if roots:
                self.root_bone = roots[0].name

        if context.collection in obj.users_collection:
            self.target_collection = context.collection.name
        elif obj.users_collection:
            self.target_collection = obj.users_collection[0].name
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "root_bone")
        layout.prop(self, "target_collection")
        layout.separator()
        layout.prop(self, "range_mode")
        layout.prop(self, "sample_step")
        layout.prop(self, "dry_run")
        box = layout.box()
        box.label(text="The original armature is restored after export.", icon='INFO')
        box.label(text="The active Action is converted on the temporary copy.")

    def execute(self, context):
        source = context.active_object
        collection = bpy.data.collections.get(self.target_collection)
        try:
            summary = run_root_motion_export(
                context,
                source,
                self.root_bone,
                collection,
                range_mode=self.range_mode,
                sample_step=self.sample_step,
                dry_run=self.dry_run,
            )
        except (RootMotionExportError, RuntimeError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

        if self.dry_run:
            message = (
                f'Validated "{summary["object_name"]}" over '
                f'{summary["frame_count"]} samples; no file was exported'
            )
        else:
            message = (
                f'Exported "{summary["object_name"]}" with the saved exporters '
                f'({summary["frame_count"]} samples)'
            )
        self.report({'INFO'}, message)
        return {'FINISHED'}


class ROOTMOTIONEXPORT_PT_panel(Panel):
    bl_label = "Root Motion Export"
    bl_idname = "ROOTMOTIONEXPORT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animation"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None or obj.type != 'ARMATURE':
            layout.label(text="Select an armature", icon='ARMATURE_DATA')
            return
        layout.operator(
            ROOTMOTIONEXPORT_OT_export.bl_idname,
            text="Export Root Motion Copy",
            icon='EXPORT',
        )


_CLASSES = (
    ROOTMOTIONEXPORT_OT_export,
    ROOTMOTIONEXPORT_PT_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
