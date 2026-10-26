# SPDX-License-Identifier: GPL-2.0-or-later

"""Bake armature-object animation into the active root pose bone.

The operator preserves the evaluated world-space motion of the root bone at
every sampled frame.  Existing root-bone animation is included in the bake,
while transform F-curves on the armature object are removed from the active
Action.
"""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Matrix


bl_info = {
    "name": "Object Animation to Root Bone",
    "author": "OpenAI",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Animation > Object to Root Bone",
    "description": (
        "Bake an armature object's transform animation into its active root bone"
    ),
    "category": "Animation",
}


_MAX_SAMPLES = 250_000
_MATRIX_TOLERANCE = 2.0e-4
_BACKUP_FLAG = "_object_animation_to_root_backup"
_BACKUP_SOURCE = "_object_animation_to_root_source"

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


class ObjectToRootError(RuntimeError):
    pass


def _bone_path(bone_name):
    escaped = bpy.utils.escape_identifier(bone_name)
    return f'pose.bones["{escaped}"]'


def _bone_transform_paths(bone_name):
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
            raise ObjectToRootError(
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
        raise ObjectToRootError(
            f'No editable channel bag was found in action "{action.name}"'
        )
    if len(bags) != 1:
        raise ObjectToRootError(
            f'Action "{action.name}" has {len(bags)} contributing channel bags '
            "for this armature. Consolidate it to one layer/strip before baking"
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
        start = float(scene.frame_start)
        end = float(scene.frame_end)
    elif getattr(action, "use_frame_range", False):
        start = float(action.frame_start)
        end = float(action.frame_end)
    else:
        curve_range = action.curve_frame_range
        start = float(curve_range[0])
        end = float(curve_range[1])
        if start == end:
            start = float(scene.frame_start)
            end = float(scene.frame_end)

    if not math.isfinite(start) or not math.isfinite(end):
        raise ObjectToRootError("Animation frame range is not finite")
    if end < start:
        start, end = end, start
    return start, end


def _sample_frames(scene, action, slot, bone_name, range_mode, step):
    if not math.isfinite(step) or step <= 0.0:
        raise ObjectToRootError("Sample step must be greater than zero")

    start, end = _animation_range(scene, action, range_mode)
    regular_count = int(math.floor(max(0.0, end - start) / step)) + 1
    if regular_count > _MAX_SAMPLES:
        raise ObjectToRootError(
            f"The requested range needs {regular_count:,} samples; "
            f"the safety limit is {_MAX_SAMPLES:,}"
        )

    frames = {start, end}
    frames.update(start + index * step for index in range(regular_count))
    relevant_paths = _OBJECT_TRANSFORM_PATHS | _bone_transform_paths(bone_name)
    frames.update(
        frame
        for frame in _curve_key_frames(action, slot, relevant_paths)
        if start <= frame <= end
    )
    frames = sorted(frames)

    if len(frames) > _MAX_SAMPLES:
        raise ObjectToRootError(
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


def _decompose_basis(matrix, frame):
    if abs(float(matrix.determinant())) <= 1.0e-12:
        raise ObjectToRootError(
            f"Root transform is singular at frame {frame:g}; check zero scale"
        )

    location, rotation, scale = matrix.decompose()
    rotation.normalize()
    reconstructed = Matrix.LocRotScale(location, rotation, scale)
    error = _matrix_error(matrix, reconstructed)
    if not math.isfinite(error) or error > _MATRIX_TOLERANCE:
        raise ObjectToRootError(
            f"Object/root transforms produce unsupported shear at frame {frame:g} "
            f"(matrix error {error:.3g})"
        )
    return location, rotation, scale


def _has_effective_nla(animation_data):
    if animation_data is None or not getattr(animation_data, "use_nla", True):
        return False
    for track in animation_data.nla_tracks:
        if track.mute:
            continue
        for strip in track.strips:
            if not strip.mute and float(strip.influence) != 0.0:
                return True
    return False


def _has_effective_constraints(owner):
    return any(
        not constraint.mute and float(constraint.influence) != 0.0
        for constraint in owner.constraints
    )


def _is_descendant_of(bone, ancestor):
    parent = bone.parent
    while parent is not None:
        if parent == ancestor:
            return True
        parent = parent.parent
    return False


def _validate(context, obj, bone_name, range_mode, sample_step):
    if obj is None or obj.type != 'ARMATURE':
        raise ObjectToRootError("The active object must be an armature")
    if obj.library is not None or obj.data.library is not None:
        raise ObjectToRootError(
            "The armature is linked and read-only; make it local first"
        )
    if obj.parent is not None:
        raise ObjectToRootError(
            "The armature object is parented. Clear/bake its object parent first"
        )
    if _has_effective_constraints(obj):
        raise ObjectToRootError(
            "The armature object has active constraints. Bake or remove them first"
        )

    bone = obj.data.bones.get(bone_name)
    pose_bone = obj.pose.bones.get(bone_name)
    if bone is None or pose_bone is None:
        raise ObjectToRootError("Select an active pose bone")
    if bone.parent is not None:
        raise ObjectToRootError(
            f'Bone "{bone_name}" is parented; select the parentless root bone'
        )
    outside = [
        candidate.name
        for candidate in obj.data.bones
        if candidate != bone and not _is_descendant_of(candidate, bone)
    ]
    if outside:
        preview = ", ".join(outside[:4])
        if len(outside) > 4:
            preview += ", ..."
        raise ObjectToRootError(
            f'Bone "{bone_name}" is not the root of the whole armature '
            f"(outside its hierarchy: {preview})"
        )
    if _has_effective_constraints(pose_bone):
        raise ObjectToRootError(
            "The active root bone has active constraints. Bake or remove them first"
        )

    animation_data = obj.animation_data
    action = animation_data.action if animation_data is not None else None
    if action is None:
        raise ObjectToRootError("The armature needs an active Action")
    if not getattr(action, "is_editable", True):
        raise ObjectToRootError(f'Action "{action.name}" is linked or read-only')
    real_users = int(action.users) - int(bool(getattr(action, "use_fake_user", False)))
    if real_users > 1:
        raise ObjectToRootError(
            f'Action "{action.name}" is shared by {real_users} data-blocks. '
            "Make it single-user before baking"
        )
    if _has_effective_nla(animation_data):
        raise ObjectToRootError(
            "The armature has active NLA strips. Bake or mute the NLA stack first"
        )

    root_paths = _bone_transform_paths(bone_name)
    for driver in animation_data.drivers:
        if driver.data_path in _OBJECT_TRANSFORM_PATHS:
            raise ObjectToRootError(
                "The armature object's transform has drivers; bake them first"
            )
        if driver.data_path in root_paths:
            raise ObjectToRootError(
                "The active root bone's transform has drivers; bake them first"
            )

    slot = getattr(animation_data, "action_slot", None)
    bags = _channelbags_for_slot(action, slot)
    has_object_motion = any(
        curve.data_path in _OBJECT_TRANSFORM_PATHS
        for fcurves, _groups, _is_layered in bags
        for curve in fcurves
    )
    if not has_object_motion:
        raise ObjectToRootError(
            f'Action "{action.name}" has no armature-object transform curves'
        )

    frames = _sample_frames(
        context.scene,
        action,
        slot,
        bone_name,
        range_mode,
        sample_step,
    )
    return action, slot, frames


def _sample_world_root(context, obj, bone_name, frames):
    samples = []
    first_object_world = None
    for frame in frames:
        _set_scene_frame(context.scene, frame)
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        pose_bone = evaluated.pose.bones.get(bone_name)
        if pose_bone is None:
            raise ObjectToRootError(
                f'Evaluated pose bone "{bone_name}" is unavailable'
            )
        if first_object_world is None:
            first_object_world = evaluated.matrix_world.copy()
        samples.append(evaluated.matrix_world @ pose_bone.matrix)
    return samples, first_object_world


def _prepare_bone_samples(
    obj,
    bone_name,
    frames,
    world_samples,
    target_world,
    rotation_mode,
):
    if abs(float(target_world.determinant())) <= 1.0e-12:
        raise ObjectToRootError(
            "The target object transform is singular; check zero scale"
        )

    target_inverse = target_world.inverted()
    rest_inverse = obj.data.bones[bone_name].matrix_local.inverted()
    prepared = []
    previous_rotation = None
    previous_euler = None
    previous_axis = None

    for frame, world_matrix in zip(frames, world_samples):
        desired_pose = target_inverse @ world_matrix
        desired_basis = rest_inverse @ desired_pose
        location, rotation, scale = _decompose_basis(desired_basis, frame)

        if previous_rotation is not None and rotation.dot(previous_rotation) < 0.0:
            rotation.negate()
        previous_rotation = rotation.copy()

        if rotation_mode == 'QUATERNION':
            rotation_values = tuple(float(value) for value in rotation)
        elif rotation_mode == 'AXIS_ANGLE':
            axis, angle = rotation.to_axis_angle()
            if previous_axis is not None and axis.dot(previous_axis) < 0.0:
                axis.negate()
                angle = -angle
            previous_axis = axis.copy()
            rotation_values = (
                float(angle),
                float(axis.x),
                float(axis.y),
                float(axis.z),
            )
        else:
            if previous_euler is None:
                euler = rotation.to_euler(rotation_mode)
            else:
                euler = rotation.to_euler(rotation_mode, previous_euler)
            previous_euler = euler.copy()
            rotation_values = tuple(float(value) for value in euler)

        prepared.append(
            (
                tuple(float(value) for value in location),
                rotation_values,
                tuple(float(value) for value in scale),
            )
        )
    return prepared


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


def _write_baked_curves(
    action,
    slot,
    bone_name,
    frames,
    samples,
    rotation_mode,
):
    bags = _channelbags_for_slot(action, slot)
    remove_paths = _OBJECT_TRANSFORM_PATHS | _bone_transform_paths(bone_name)
    for fcurves, _groups, _is_layered in bags:
        for curve in list(fcurves):
            if curve.data_path in remove_paths:
                fcurves.remove(curve)

    fcurves, groups, is_layered = bags[0]
    base = _bone_path(bone_name)
    if rotation_mode == 'QUATERNION':
        rotation_path = f"{base}.rotation_quaternion"
        rotation_channels = [[], [], [], []]
    elif rotation_mode == 'AXIS_ANGLE':
        rotation_path = f"{base}.rotation_axis_angle"
        rotation_channels = [[], [], [], []]
    else:
        rotation_path = f"{base}.rotation_euler"
        rotation_channels = [[], [], []]

    channels = {
        f"{base}.location": [[], [], []],
        rotation_path: rotation_channels,
        f"{base}.scale": [[], [], []],
    }
    for location, rotation, scale in samples:
        for index, value in enumerate(location):
            channels[f"{base}.location"][index].append(value)
        for index, value in enumerate(rotation):
            channels[rotation_path][index].append(value)
        for index, value in enumerate(scale):
            channels[f"{base}.scale"][index].append(value)

    for data_path, components in channels.items():
        for array_index, values in enumerate(components):
            curve = _new_fcurve(
                fcurves,
                groups,
                is_layered,
                data_path,
                array_index,
                bone_name,
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
            if getattr(action, "use_cyclic", False):
                curve.modifiers.new('CYCLES')
    action.update_tag()


def _create_action_backup(action, obj, bone_name):
    backup = action.copy()
    backup.name = f"{action.name}__before_object_to_root"
    backup.use_fake_user = True
    backup[_BACKUP_FLAG] = True
    backup[_BACKUP_SOURCE] = action.name
    backup["armature_object"] = obj.name
    backup["root_bone"] = bone_name
    return backup


def _set_object_world_transform(obj, target_world):
    obj.delta_location = (0.0, 0.0, 0.0)
    obj.delta_rotation_euler = (0.0, 0.0, 0.0)
    obj.delta_rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    obj.delta_scale = (1.0, 1.0, 1.0)
    obj.matrix_world = target_world


def transfer_object_animation_to_root(
    context,
    obj,
    bone_name,
    *,
    range_mode='ACTION',
    sample_step=1.0,
    target_mode='CURRENT',
    create_backup=True,
):
    """Bake the active Action's object transforms into ``bone_name``."""

    original_frame = context.scene.frame_current
    original_subframe = context.scene.frame_subframe

    try:
        context.view_layer.update()
        action, slot, frames = _validate(
            context,
            obj,
            bone_name,
            range_mode,
            sample_step,
        )
        current_world = obj.evaluated_get(
            context.evaluated_depsgraph_get()
        ).matrix_world.copy()
        world_samples, first_world = _sample_world_root(
            context,
            obj,
            bone_name,
            frames,
        )
        rotation_mode = obj.pose.bones[bone_name].rotation_mode

        if target_mode == 'IDENTITY':
            target_world = Matrix.Identity(4)
        elif target_mode == 'FIRST_FRAME':
            target_world = first_world
        elif target_mode == 'CURRENT':
            target_world = current_world
        else:
            raise ObjectToRootError(f'Unknown object result mode: "{target_mode}"')

        prepared = _prepare_bone_samples(
            obj,
            bone_name,
            frames,
            world_samples,
            target_world,
            rotation_mode,
        )

        if create_backup:
            _create_action_backup(action, obj, bone_name)

        _write_baked_curves(
            action,
            slot,
            bone_name,
            frames,
            prepared,
            rotation_mode,
        )
        _set_object_world_transform(obj, target_world)
        context.view_layer.update()
        return {
            "action_name": action.name,
            "bone_name": bone_name,
            "frame_count": len(frames),
            "frame_start": frames[0],
            "frame_end": frames[-1],
            "target_mode": target_mode,
            "backup_created": bool(create_backup),
        }
    finally:
        _set_scene_frame(
            context.scene,
            float(original_frame) + float(original_subframe),
        )
        context.view_layer.update()


class OBJECTTOROOT_PG_settings(PropertyGroup):
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
    target_mode: EnumProperty(
        name="Object Result",
        description="Static transform left on the armature object after baking",
        items=(
            (
                'CURRENT',
                "Keep Current Transform",
                "Freeze the armature object at its transform on the current frame",
            ),
            (
                'FIRST_FRAME',
                "Keep First-Frame Transform",
                "Freeze the armature object at the first sampled frame",
            ),
            (
                'IDENTITY',
                "Reset to Identity",
                "Set the armature object to zero location/rotation and unit scale",
            ),
        ),
        default='CURRENT',
    )
    create_backup: BoolProperty(
        name="Create Action Backup",
        description="Keep a fake-user copy of the Action before conversion",
        default=True,
    )


class OBJECTTOROOT_OT_transfer(Operator):
    bl_idname = "anim.object_animation_to_active_root"
    bl_label = "Transfer Object Animation to Root"
    bl_description = (
        "Bake armature-object animation into the active parentless pose bone"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == 'ARMATURE'
            and context.mode == 'POSE'
            and context.active_pose_bone is not None
        )

    def execute(self, context):
        obj = context.active_object
        bone_name = context.active_pose_bone.name
        settings = context.scene.object_animation_to_root

        try:
            summary = transfer_object_animation_to_root(
                context,
                obj,
                bone_name,
                range_mode=settings.range_mode,
                sample_step=settings.sample_step,
                target_mode=settings.target_mode,
                create_backup=settings.create_backup,
            )
        except (ObjectToRootError, RuntimeError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        except Exception as error:
            self.report({'ERROR'}, f"Object-to-root transfer failed: {error}")
            return {'CANCELLED'}

        backup_text = " and created an Action backup" if settings.create_backup else ""
        self.report(
            {'INFO'},
            f'Baked {summary["frame_count"]} frame(s) to "{bone_name}"'
            f"{backup_text}",
        )
        return {'FINISHED'}


class OBJECTTOROOT_PT_panel(Panel):
    bl_label = "Object to Root Bone"
    bl_idname = "OBJECTTOROOT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animation"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        bone_name = _active_bone_name(obj)
        settings = context.scene.object_animation_to_root

        if context.mode != 'POSE':
            layout.label(text="Enter Pose Mode and select the root", icon='INFO')
            return
        if not bone_name:
            layout.label(text="Select an active root bone", icon='INFO')
            return

        bone = obj.data.bones.get(bone_name)
        layout.label(text=f"Active Bone: {bone_name}", icon='BONE_DATA')
        if bone is not None and bone.parent is not None:
            layout.label(text="The active bone is not parentless", icon='ERROR')

        layout.prop(settings, "range_mode")
        layout.prop(settings, "sample_step")
        layout.prop(settings, "target_mode")
        layout.prop(settings, "create_backup")

        box = layout.box()
        box.label(text="Object and existing root motion are combined.", icon='INFO')
        box.label(text="The root bone's rotation mode is preserved.")

        row = layout.row()
        row.enabled = bone is not None and bone.parent is None
        row.operator(OBJECTTOROOT_OT_transfer.bl_idname, icon='ACTION')


_CLASSES = (
    OBJECTTOROOT_PG_settings,
    OBJECTTOROOT_OT_transfer,
    OBJECTTOROOT_PT_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.object_animation_to_root = PointerProperty(
        type=OBJECTTOROOT_PG_settings
    )


def unregister():
    del bpy.types.Scene.object_animation_to_root
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
