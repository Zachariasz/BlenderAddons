# SPDX-License-Identifier: GPL-2.0-or-later

import math

import bpy
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Euler, Matrix, Quaternion, Vector


bl_info = {
    "name": "Root Bone Reorient",
    "author": "OpenAI",
    "version": (1, 0, 1),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Animation > Root Bone Reorient",
    "description": "Reorient a root bone while preserving root motion in all matching actions",
    "category": "Animation",
}


_BACKUP_FLAG = "_root_bone_reorient_backup"
_BACKUP_SOURCE = "_root_bone_reorient_source"
_MAX_SAMPLES_PER_TARGET = 250_000
_MATRIX_TOLERANCE = 1.0e-5
_VECTOR_EPSILON = 1.0e-8

_TRANSFORM_PROPERTIES = (
    "location",
    "rotation_euler",
    "rotation_quaternion",
    "rotation_axis_angle",
    "scale",
)


class RootReorientError(RuntimeError):
    pass


class _CurveTarget:
    __slots__ = (
        "action",
        "fcurves",
        "groups",
        "is_layered",
        "label",
        "slot",
    )

    def __init__(self, action, fcurves, groups, is_layered, label, slot=None):
        self.action = action
        self.fcurves = fcurves
        self.groups = groups
        self.is_layered = is_layered
        self.label = label
        self.slot = slot


class _PreparedTarget:
    __slots__ = ("target", "frames", "channels", "group_name")

    def __init__(self, target, frames, channels, group_name):
        self.target = target
        self.frames = frames
        self.channels = channels
        self.group_name = group_name


def _bone_data_path(bone_name):
    escaped_name = bpy.utils.escape_identifier(bone_name)
    return f'pose.bones["{escaped_name}"]'


def _transform_paths(bone_path):
    return {f"{bone_path}.{prop}" for prop in _TRANSFORM_PROPERTIES}


def _active_bone_name(obj):
    if obj is None or obj.type != 'ARMATURE':
        return None

    if obj.mode == 'EDIT':
        bone = obj.data.edit_bones.active
    else:
        bone = obj.data.bones.active
    return bone.name if bone is not None else None


def _pose_bone_selection_supported():
    return "select" in bpy.types.PoseBone.bl_rna.properties


def _is_backup_action(action):
    try:
        return bool(action.get(_BACKUP_FLAG, False))
    except (AttributeError, TypeError):
        return False


def _is_root_transform_curve(fcurve, transform_paths):
    return fcurve.data_path in transform_paths


def _find_matching_targets(bone_path):
    """Return every legacy action or layered channel bag animating bone_path."""
    transform_paths = _transform_paths(bone_path)
    targets = []

    for action in bpy.data.actions:
        if _is_backup_action(action):
            continue

        if getattr(action, "is_action_layered", False):
            for layer_index, layer in enumerate(action.layers):
                for strip_index, strip in enumerate(layer.strips):
                    channelbags = getattr(strip, "channelbags", None)
                    if channelbags is None:
                        continue

                    for channelbag in channelbags:
                        if not any(
                            _is_root_transform_curve(fc, transform_paths)
                            for fc in channelbag.fcurves
                        ):
                            continue

                        slot = getattr(channelbag, "slot", None)
                        slot_name = None
                        if slot is not None:
                            slot_name = (
                                getattr(slot, "name_display", None)
                                or getattr(slot, "identifier", None)
                            )
                        label = action.name
                        if slot_name:
                            label = f"{label} [{slot_name}]"
                        if len(action.layers) > 1 or len(layer.strips) > 1:
                            label = f"{label} (L{layer_index + 1}/S{strip_index + 1})"

                        targets.append(
                            _CurveTarget(
                                action,
                                channelbag.fcurves,
                                channelbag.groups,
                                True,
                                label,
                                slot,
                            )
                        )
        else:
            fcurves = getattr(action, "fcurves", None)
            if fcurves is None:
                continue
            if any(_is_root_transform_curve(fc, transform_paths) for fc in fcurves):
                targets.append(
                    _CurveTarget(
                        action,
                        fcurves,
                        getattr(action, "groups", None),
                        False,
                        action.name,
                    )
                )

    return targets


def _curve_has_time_data(fcurve):
    return bool(fcurve.keyframe_points or fcurve.sampled_points)


def _target_frames(target, transform_paths):
    action = target.action

    if getattr(action, "use_frame_range", False):
        start = float(action.frame_start)
        end = float(action.frame_end)
    else:
        ranges = []
        for fcurve in target.fcurves:
            if not _curve_has_time_data(fcurve):
                continue
            curve_range = fcurve.range()
            ranges.append((float(curve_range[0]), float(curve_range[1])))

        if not ranges:
            raise RootReorientError(
                f"{target.label}: no finite keyed frame range could be found"
            )
        start = min(item[0] for item in ranges)
        end = max(item[1] for item in ranges)

    if not math.isfinite(start) or not math.isfinite(end):
        raise RootReorientError(f"{target.label}: frame range is not finite")
    if end < start:
        start, end = end, start

    frames = {start, end}
    first_integer = math.ceil(start)
    last_integer = math.floor(end)
    if last_integer >= first_integer:
        integer_count = last_integer - first_integer + 1
        if integer_count > _MAX_SAMPLES_PER_TARGET:
            raise RootReorientError(
                f"{target.label}: frame range requires {integer_count:,} samples; "
                f"the safety limit is {_MAX_SAMPLES_PER_TARGET:,}"
            )
        frames.update(float(frame) for frame in range(first_integer, last_integer + 1))

    for fcurve in target.fcurves:
        if not _is_root_transform_curve(fcurve, transform_paths):
            continue
        for point in fcurve.keyframe_points:
            frame = float(point.co[0])
            if math.isfinite(frame) and start <= frame <= end:
                frames.add(frame)
        for point in fcurve.sampled_points:
            frame = float(point.co[0])
            if math.isfinite(frame) and start <= frame <= end:
                frames.add(frame)

    if len(frames) > _MAX_SAMPLES_PER_TARGET:
        raise RootReorientError(
            f"{target.label}: requires {len(frames):,} samples; "
            f"the safety limit is {_MAX_SAMPLES_PER_TARGET:,}"
        )

    return sorted(frames)


def _fcurve_map(target):
    curves = {}
    for fcurve in target.fcurves:
        key = (fcurve.data_path, int(fcurve.array_index))
        curves.setdefault(key, fcurve)
    return curves


def _evaluate_component(curves, data_path, index, default, frame):
    fcurve = curves.get((data_path, index))
    if fcurve is None:
        return default
    value = float(fcurve.evaluate(frame))
    if not math.isfinite(value):
        raise RootReorientError(
            f"Non-finite value in {data_path}[{index}] at frame {frame:g}"
        )
    return value


def _normalized_quaternion(values):
    rotation = Quaternion(values)
    if rotation.magnitude <= _VECTOR_EPSILON:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    rotation.normalize()
    return rotation


def _sample_basis(curves, bone_path, rotation_mode, frame):
    location_path = f"{bone_path}.location"
    scale_path = f"{bone_path}.scale"
    location = Vector(
        _evaluate_component(curves, location_path, index, 0.0, frame)
        for index in range(3)
    )
    scale = Vector(
        _evaluate_component(curves, scale_path, index, 1.0, frame)
        for index in range(3)
    )

    if rotation_mode == 'QUATERNION':
        path = f"{bone_path}.rotation_quaternion"
        values = [
            _evaluate_component(curves, path, index, 1.0 if index == 0 else 0.0, frame)
            for index in range(4)
        ]
        rotation = _normalized_quaternion(values)
    elif rotation_mode == 'AXIS_ANGLE':
        path = f"{bone_path}.rotation_axis_angle"
        defaults = (0.0, 0.0, 0.0, 1.0)
        values = [
            _evaluate_component(curves, path, index, defaults[index], frame)
            for index in range(4)
        ]
        axis = Vector(values[1:4])
        if axis.length_squared <= _VECTOR_EPSILON:
            axis = Vector((0.0, 0.0, 1.0))
        else:
            axis.normalize()
        rotation = Quaternion(axis, values[0])
        rotation.normalize()
    else:
        path = f"{bone_path}.rotation_euler"
        values = [
            _evaluate_component(curves, path, index, 0.0, frame)
            for index in range(3)
        ]
        rotation = Euler(values, rotation_mode).to_quaternion()

    return Matrix.LocRotScale(location, rotation, scale)


def _matrix_error(left, right):
    return max(
        abs(float(left[row][column]) - float(right[row][column]))
        for row in range(4)
        for column in range(4)
    )


def _matrix_scale(matrix):
    return max(
        1.0,
        max(
            abs(float(matrix[row][column]))
            for row in range(4)
            for column in range(4)
        ),
    )


def _prepare_target(target, bone_path, bone_name, rotation_mode, delta):
    transform_paths = _transform_paths(bone_path)
    frames = _target_frames(target, transform_paths)
    curves = _fcurve_map(target)
    delta_inverse = delta.inverted()

    location_path = f"{bone_path}.location"
    scale_path = f"{bone_path}.scale"
    channels = {
        location_path: [[], [], []],
        scale_path: [[], [], []],
    }

    if rotation_mode == 'QUATERNION':
        rotation_path = f"{bone_path}.rotation_quaternion"
        channels[rotation_path] = [[], [], [], []]
    elif rotation_mode == 'AXIS_ANGLE':
        rotation_path = f"{bone_path}.rotation_axis_angle"
        channels[rotation_path] = [[], [], [], []]
    else:
        rotation_path = f"{bone_path}.rotation_euler"
        channels[rotation_path] = [[], [], []]

    previous_quaternion = None
    previous_euler = None
    previous_axis = Vector((0.0, 0.0, 1.0))

    for frame in frames:
        old_basis = _sample_basis(curves, bone_path, rotation_mode, frame)
        new_basis = delta_inverse @ old_basis @ delta
        location, rotation, scale = new_basis.decompose()
        rotation.normalize()

        reconstructed = Matrix.LocRotScale(location, rotation, scale)
        error = _matrix_error(new_basis, reconstructed)
        allowed_error = _MATRIX_TOLERANCE * _matrix_scale(new_basis)
        if not math.isfinite(error) or error > allowed_error:
            raise RootReorientError(
                f"{target.label}: frame {frame:g} produces shear that cannot be "
                "represented by Blender location/rotation/scale channels"
            )

        if previous_quaternion is not None and previous_quaternion.dot(rotation) < 0.0:
            rotation.negate()

        if rotation_mode == 'QUATERNION':
            rotation_values = tuple(float(value) for value in rotation)
        elif rotation_mode == 'AXIS_ANGLE':
            axis, angle = rotation.to_axis_angle()
            if previous_axis.dot(axis) < 0.0:
                axis.negate()
                angle = -angle
            previous_axis = axis.copy()
            rotation_values = (float(angle), float(axis.x), float(axis.y), float(axis.z))
        else:
            if previous_euler is None:
                euler = rotation.to_euler(rotation_mode)
            else:
                euler = rotation.to_euler(rotation_mode, previous_euler)
            previous_euler = euler.copy()
            rotation_values = tuple(float(value) for value in euler)

        previous_quaternion = rotation.copy()

        for index, value in enumerate(location):
            channels[location_path][index].append(float(value))
        for index, value in enumerate(rotation_values):
            channels[rotation_path][index].append(float(value))
        for index, value in enumerate(scale):
            channels[scale_path][index].append(float(value))

    group_name = bone_name
    for fcurve in target.fcurves:
        if not _is_root_transform_curve(fcurve, transform_paths):
            continue
        group = getattr(fcurve, "group", None)
        if group is not None:
            group_name = group.name
            break

    return _PreparedTarget(target, frames, channels, group_name)


def _ensure_layered_group(target, name):
    if not name or target.groups is None:
        return None
    group = target.groups.get(name)
    if group is None:
        group = target.groups.new(name)
    return group


def _new_fcurve(target, data_path, index, group_name):
    if target.is_layered:
        fcurve = target.fcurves.new(data_path, index=index)
        group = _ensure_layered_group(target, group_name)
        if group is not None:
            fcurve.group = group
        return fcurve

    return target.fcurves.new(
        data_path,
        index=index,
        action_group=group_name or "",
    )


def _write_prepared_target(prepared, bone_path):
    target = prepared.target
    transform_paths = _transform_paths(bone_path)

    for fcurve in list(target.fcurves):
        if _is_root_transform_curve(fcurve, transform_paths):
            target.fcurves.remove(fcurve)

    flattened_frames = prepared.frames
    for data_path, component_values in prepared.channels.items():
        for index, values in enumerate(component_values):
            fcurve = _new_fcurve(target, data_path, index, prepared.group_name)
            fcurve.extrapolation = 'CONSTANT'
            fcurve.keyframe_points.add(count=len(flattened_frames))

            coordinates = []
            for frame, value in zip(flattened_frames, values):
                coordinates.extend((frame, value))
            fcurve.keyframe_points.foreach_set("co", coordinates)

            for point in fcurve.keyframe_points:
                point.interpolation = 'LINEAR'
                point.handle_left_type = 'VECTOR'
                point.handle_right_type = 'VECTOR'
            fcurve.update()

            if getattr(target.action, "use_cyclic", False):
                fcurve.modifiers.new('CYCLES')

    target.action.update_tag()


def _capture_bone_selection(obj):
    if obj.mode == 'EDIT':
        active = obj.data.edit_bones.active
        selected = {
            bone.name for bone in obj.data.edit_bones if bone.select
        }
    elif _pose_bone_selection_supported():
        active = obj.data.bones.active
        selected = {
            pose_bone.name for pose_bone in obj.pose.bones if pose_bone.select
        }
    else:
        active = obj.data.bones.active
        selected = {
            bone.name for bone in obj.data.bones if bone.select
        }

    return (
        active.name if active is not None else None,
        selected,
    )


def _restore_bone_selection(obj, active_name, selected_names):
    if obj.mode == 'EDIT':
        for bone in obj.data.edit_bones:
            bone.select = bone.name in selected_names
        if active_name:
            active = obj.data.edit_bones.get(active_name)
            if active is not None:
                obj.data.edit_bones.active = active
    elif _pose_bone_selection_supported():
        for pose_bone in obj.pose.bones:
            pose_bone.select = pose_bone.name in selected_names
        if active_name:
            active = obj.data.bones.get(active_name)
            if active is not None:
                obj.data.bones.active = active
    else:
        for bone in obj.data.bones:
            bone.select = bone.name in selected_names
        if active_name:
            active = obj.data.bones.get(active_name)
            if active is not None:
                obj.data.bones.active = active


def _edit_rest_bone(obj, bone_name, target_tail, target_roll, commit):
    """Measure or apply a rest edit, restoring both the edit and mode for previews."""
    original_mode = obj.mode
    edit_bone = None
    old_tail = None
    old_roll = None
    should_restore_edit = not commit

    try:
        if obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')

        edit_bone = obj.data.edit_bones.get(bone_name)
        if edit_bone is None:
            raise RootReorientError(f'Edit bone "{bone_name}" is unavailable')

        old_head = edit_bone.head.copy()
        old_tail = edit_bone.tail.copy()
        old_roll = float(edit_bone.roll)
        old_matrix = edit_bone.matrix.copy()

        edit_bone.tail = Vector(target_tail)
        edit_bone.roll = float(target_roll)
        new_matrix = edit_bone.matrix.copy()

        result = {
            "old_head": old_head,
            "old_tail": old_tail,
            "old_roll": old_roll,
            "old_matrix": old_matrix,
            "new_matrix": new_matrix,
        }
        return result
    finally:
        if should_restore_edit and edit_bone is not None and old_tail is not None:
            edit_bone.tail = old_tail
            edit_bone.roll = old_roll
        if obj.mode != original_mode:
            bpy.ops.object.mode_set(mode=original_mode)


def _restore_rest_bone(obj, bone_name, old_tail, old_roll, original_mode):
    current_mode = obj.mode
    try:
        if current_mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        edit_bone = obj.data.edit_bones.get(bone_name)
        if edit_bone is not None:
            edit_bone.tail = old_tail
            edit_bone.roll = old_roll
    finally:
        if obj.mode != original_mode:
            bpy.ops.object.mode_set(mode=original_mode)


def _validate_context(obj, bone_name, settings):
    if obj is None or obj.type != 'ARMATURE':
        raise RootReorientError("The active object must be an armature")
    if not getattr(obj, "is_editable", True) or not getattr(obj.data, "is_editable", True):
        raise RootReorientError("The active armature is linked or read-only")

    bone = obj.data.bones.get(bone_name)
    if bone is None:
        raise RootReorientError("Select an active armature bone")
    if bone.parent is not None:
        raise RootReorientError(f'Bone "{bone_name}" is parented; select the parentless root')

    connected_children = [child.name for child in bone.children if child.use_connect]
    if connected_children:
        names = ", ".join(connected_children[:4])
        if len(connected_children) > 4:
            names += ", ..."
        raise RootReorientError(
            "The root has connected children whose rest geometry would move: " + names
        )

    target_tail = Vector(settings.target_tail)
    if (target_tail - bone.head_local).length <= _VECTOR_EPSILON:
        raise RootReorientError("Target tail must differ from the root head")

    pose_bone = obj.pose.bones.get(bone_name)
    if pose_bone is None:
        raise RootReorientError(f'Pose bone "{bone_name}" is unavailable')
    if pose_bone.constraints:
        raise RootReorientError(
            "Remove or bake the root bone constraints before reorienting it"
        )

    bone_path = _bone_data_path(bone_name)
    transform_paths = _transform_paths(bone_path)
    animation_data = obj.animation_data
    if animation_data is not None:
        driven_paths = [
            driver.data_path
            for driver in animation_data.drivers
            if driver.data_path in transform_paths
        ]
        if driven_paths:
            raise RootReorientError(
                "Remove or bake the root transform drivers before reorienting it"
            )

    return bone, pose_bone, bone_path


def _create_action_backups(actions, bone_name, rest_data):
    backups = []
    for action in sorted(actions, key=lambda item: item.name.casefold()):
        backup = action.copy()
        backup.name = f"{action.name}__before_root_reorient"
        backup.use_fake_user = True
        backup[_BACKUP_FLAG] = True
        backup[_BACKUP_SOURCE] = action.name
        backup["root_bone"] = bone_name
        backup["old_head"] = list(rest_data["old_head"])
        backup["old_tail"] = list(rest_data["old_tail"])
        backup["old_roll"] = rest_data["old_roll"]
        backups.append(backup)
    return backups


class ROOTREORIENT_PG_settings(PropertyGroup):
    target_tail: FloatVectorProperty(
        name="Target Tail",
        description="New tail position in armature space; the root head stays unchanged",
        size=3,
        default=(0.0, 1.0, 0.0),
        subtype='XYZ',
    )
    target_roll: FloatProperty(
        name="Target Roll",
        description="New edit-bone roll",
        subtype='ANGLE',
        unit='ROTATION',
        default=0.0,
    )
    create_backups: BoolProperty(
        name="Create Action Backups",
        description="Keep fake-user copies of every converted action",
        default=True,
    )


class ROOTREORIENT_OT_convert(Operator):
    bl_idname = "anim.root_bone_reorient_preserve_motion"
    bl_label = "Reorient Root and Preserve Motion"
    bl_description = (
        "Change the active root bone's rest orientation and rebase every matching action"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE' and _active_bone_name(obj) is not None

    def execute(self, context):
        obj = context.active_object
        bone_name = _active_bone_name(obj)
        settings = context.scene.root_bone_reorient
        original_mode = obj.mode
        active_bone_name, selected_bones = _capture_bone_selection(obj)
        original_frame = context.scene.frame_current
        original_subframe = context.scene.frame_subframe
        rest_data = None

        try:
            _bone, pose_bone, bone_path = _validate_context(
                obj, bone_name, settings
            )
            targets = _find_matching_targets(bone_path)
            if not targets:
                raise RootReorientError(
                    f'No actions in this file animate transforms on bone "{bone_name}"'
                )

            read_only_actions = sorted(
                {
                    target.action.name
                    for target in targets
                    if not getattr(target.action, "is_editable", True)
                }
            )
            if read_only_actions:
                raise RootReorientError(
                    "Matching actions are linked or read-only: "
                    + ", ".join(read_only_actions[:5])
                )

            rest_data = _edit_rest_bone(
                obj,
                bone_name,
                settings.target_tail,
                settings.target_roll,
                commit=False,
            )
            delta = rest_data["old_matrix"].inverted() @ rest_data["new_matrix"]

            prepared_targets = [
                _prepare_target(
                    target,
                    bone_path,
                    bone_name,
                    pose_bone.rotation_mode,
                    delta,
                )
                for target in targets
            ]

            unique_actions = {target.action for target in targets}
            if settings.create_backups:
                _create_action_backups(unique_actions, bone_name, rest_data)

            _edit_rest_bone(
                obj,
                bone_name,
                settings.target_tail,
                settings.target_roll,
                commit=True,
            )

            for prepared in prepared_targets:
                _write_prepared_target(prepared, bone_path)

            context.view_layer.update()
            total_samples = sum(len(prepared.frames) for prepared in prepared_targets)
            backup_text = " with backups" if settings.create_backups else ""
            self.report(
                {'INFO'},
                f"Converted {len(unique_actions)} action(s), {len(targets)} slot(s), "
                f"and {total_samples} sampled frame(s){backup_text}",
            )
            return {'FINISHED'}

        except RootReorientError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        except Exception as error:
            if rest_data is not None:
                try:
                    _restore_rest_bone(
                        obj,
                        bone_name,
                        rest_data["old_tail"],
                        rest_data["old_roll"],
                        original_mode,
                    )
                except Exception:
                    pass
            self.report({'ERROR'}, f"Root reorientation failed: {error}")
            return {'CANCELLED'}
        finally:
            try:
                if obj.mode != original_mode:
                    bpy.ops.object.mode_set(mode=original_mode)
                _restore_bone_selection(obj, active_bone_name, selected_bones)
                context.scene.frame_set(original_frame, subframe=original_subframe)
            except Exception:
                pass


class ROOTREORIENT_PT_panel(Panel):
    bl_label = "Root Bone Reorient"
    bl_idname = "ROOTREORIENT_PT_panel"
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
        settings = context.scene.root_bone_reorient

        if bone_name is None:
            layout.label(text="Select an active bone", icon='INFO')
            return

        bone = obj.data.bones.get(bone_name)
        layout.label(text=f"Active Root: {bone_name}", icon='BONE_DATA')
        if bone is not None and bone.parent is not None:
            layout.label(text="The active bone is not parentless", icon='ERROR')

        layout.prop(settings, "target_tail")
        layout.prop(settings, "target_roll")
        layout.prop(settings, "create_backups")

        bone_path = _bone_data_path(bone_name)
        targets = _find_matching_targets(bone_path)
        action_names = sorted(
            {target.action.name for target in targets}, key=str.casefold
        )

        box = layout.box()
        box.label(
            text=f"Matching Animations: {len(action_names)} "
            f"({len(targets)} slot{'s' if len(targets) != 1 else ''})",
            icon='ACTION',
        )
        for action_name in action_names[:8]:
            box.label(text=action_name)
        if len(action_names) > 8:
            box.label(text=f"... and {len(action_names) - 8} more")

        row = layout.row()
        row.enabled = bool(targets) and bone is not None and bone.parent is None
        row.operator(ROOTREORIENT_OT_convert.bl_idname, icon='ARMATURE_DATA')


_CLASSES = (
    ROOTREORIENT_PG_settings,
    ROOTREORIENT_OT_convert,
    ROOTREORIENT_PT_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.root_bone_reorient = PointerProperty(type=ROOTREORIENT_PG_settings)


def unregister():
    del bpy.types.Scene.root_bone_reorient
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
