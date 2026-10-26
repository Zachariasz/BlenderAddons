# SPDX-License-Identifier: GPL-2.0-or-later

bl_info = {
    "name": "StackByTag",
    "author": "OpenAI",
    "version": (1, 1, 0),
    "blender": (5, 1, 0),
    "location": "NLA Editor > Sidebar > StackByTag",
    "description": "Stack actions whose names begin with a tag on separate NLA tracks",
    "category": "Animation",
}

import re

import bpy
from bpy.props import BoolProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup


_NAME_SEPARATORS_RE = re.compile(r"[_\-|]+")
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CASE_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")


def split_name_words(value):
    """Return case-insensitive words from separators and case boundaries."""
    value = _NAME_SEPARATORS_RE.sub(" ", value.strip())
    value = _ACRONYM_BOUNDARY_RE.sub(r"\1 \2", value)
    value = _CASE_BOUNDARY_RE.sub(r"\1 \2", value)
    return tuple(word.casefold() for word in value.split() if word)


def action_name_matches_prefix(action_name, prefix):
    """Match a prefix as one or more complete, normalized name words."""
    prefix_words = split_name_words(prefix)
    if not prefix_words:
        return True

    action_words = split_name_words(action_name)
    return action_words[: len(prefix_words)] == prefix_words


def is_name_word_boundary(value, index):
    """Return whether index sits after a recognized name word."""
    if index >= len(value):
        return True
    if index <= 0:
        return False

    before = value[index - 1]
    after = value[index]
    if after in "_-|" or after.isspace():
        return True
    if (before.islower() or before.isdigit()) and after.isupper():
        return True
    if (
        before.isupper()
        and after.isupper()
        and index + 1 < len(value)
        and value[index + 1].islower()
    ):
        return True
    return False


def action_name_prefix_end(action_name, prefix):
    """Find the raw string position after a normalized leading prefix."""
    prefix_words = split_name_words(prefix)
    if not prefix_words:
        return None

    for index in range(1, len(action_name) + 1):
        if not is_name_word_boundary(action_name, index):
            continue

        candidate_words = split_name_words(action_name[:index])
        if candidate_words == prefix_words:
            return index
        if len(candidate_words) > len(prefix_words):
            return None

    return None


def replace_action_name_prefix(action_name, prefix, replacement):
    """Replace a recognized leading prefix while preserving the name suffix."""
    prefix_end = action_name_prefix_end(action_name, prefix)
    if prefix_end is None:
        return None

    replacement = replacement.strip()
    suffix = action_name[prefix_end:]
    if not replacement:
        suffix = suffix.lstrip("_-| \t")

    new_name = replacement + suffix
    return new_name or None


def action_is_empty(action):
    """Support both layered actions and older, legacy actions."""
    if hasattr(action, "is_empty"):
        return action.is_empty

    fcurves = getattr(action, "fcurves", None)
    return fcurves is not None and len(fcurves) == 0


def existing_nla_actions(animation_data):
    return {
        strip.action
        for track in animation_data.nla_tracks
        for strip in track.strips
        if strip.type == 'CLIP' and strip.action is not None
    }


def best_action_slot(strip, target):
    """Choose the layered-action slot most likely intended for target."""
    suitable_slots = list(getattr(strip, "action_suitable_slots", ()))
    if not suitable_slots:
        return None

    # An already established user relationship is the strongest signal.
    for slot in suitable_slots:
        try:
            if target in slot.users():
                return slot
        except (AttributeError, RuntimeError, TypeError):
            pass

    # Imported actions often identify their owner by the slot display name.
    target_name = target.name.casefold()
    for slot in suitable_slots:
        slot_name = getattr(slot, "name_display", "").casefold()
        if slot_name == target_name:
            return slot

    assigned_slot = getattr(strip, "action_slot", None)
    if assigned_slot in suitable_slots:
        return assigned_slot

    return suitable_slots[0]


class STACKBYTAG_PG_settings(PropertyGroup):
    prefix: StringProperty(
        name="Tag / Prefix",
        description=(
            "Leading action-name word or words to match; leave empty for all "
            "actions. Underscore, hyphen, pipe, camelCase and PascalCase "
            "create word boundaries"
        ),
        default="",
    )

    replacement_prefix: StringProperty(
        name="New Prefix",
        description=(
            "Replacement for the recognized leading prefix; leave empty to "
            "remove the prefix"
        ),
        default="",
    )

    start_frame: IntProperty(
        name="Start Frame",
        description="Frame on which every created NLA strip starts",
        default=1,
        soft_min=-1000,
        soft_max=10000,
    )

    skip_existing: BoolProperty(
        name="Skip Actions Already in NLA",
        description="Do not add another strip for an action already used by this object",
        default=True,
    )


class STACKBYTAG_OT_stack_actions(Operator):
    bl_idname = "nla.stack_actions_by_tag"
    bl_label = "Stack Matching Actions"
    bl_description = "Add each matching action to a separate NLA track on the active object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        target = context.active_object
        return target is not None and target.is_editable

    def execute(self, context):
        target = context.active_object
        settings = context.scene.stack_by_tag_settings

        matching_actions = sorted(
            (
                action
                for action in bpy.data.actions
                if not action_is_empty(action)
                and action_name_matches_prefix(action.name, settings.prefix)
            ),
            key=lambda action: action.name.casefold(),
        )

        if not matching_actions:
            label = settings.prefix.strip() or "<all>"
            self.report({'WARNING'}, f"No non-empty actions match prefix: {label}")
            return {'CANCELLED'}

        animation_data = target.animation_data_create()
        already_used = existing_nla_actions(animation_data) if settings.skip_existing else set()

        created_actions = []
        skipped_existing = 0
        skipped_incompatible = 0
        failed = []

        for action in matching_actions:
            if action in already_used:
                skipped_existing += 1
                continue

            track = animation_data.nla_tracks.new()
            track.name = action.name

            try:
                strip = track.strips.new(action.name, settings.start_frame, action)

                action_slots = getattr(action, "slots", None)
                if action_slots is not None and len(action_slots) > 0:
                    slot = best_action_slot(strip, target)
                    if slot is None:
                        animation_data.nla_tracks.remove(track)
                        skipped_incompatible += 1
                        continue
                    strip.action_slot = slot

                strip.blend_type = 'REPLACE'
                strip.extrapolation = 'NOTHING'
                strip.influence = 1.0
                created_actions.append(action)
            except (RuntimeError, TypeError, ValueError) as exc:
                animation_data.nla_tracks.remove(track)
                failed.append(f"{action.name}: {exc}")

        # Do not let an action duplicated in the new stack continue to override
        # the NLA result from the active Action track.
        if animation_data.action in created_actions:
            animation_data.action = None

        if created_actions and hasattr(animation_data, "use_nla"):
            animation_data.use_nla = True

        if not created_actions:
            details = []
            if skipped_existing:
                details.append(f"{skipped_existing} already in NLA")
            if skipped_incompatible:
                details.append(f"{skipped_incompatible} incompatible")
            if failed:
                details.append(f"{len(failed)} failed")
            suffix = f" ({', '.join(details)})" if details else ""
            self.report({'WARNING'}, "No tracks added" + suffix)
            return {'CANCELLED'}

        summary = f"Added {len(created_actions)} action(s) to {target.name}"
        omissions = []
        if skipped_existing:
            omissions.append(f"{skipped_existing} already present")
        if skipped_incompatible:
            omissions.append(f"{skipped_incompatible} incompatible")
        if failed:
            omissions.append(f"{len(failed)} failed")
        if omissions:
            summary += f"; skipped {', '.join(omissions)}"

        self.report({'WARNING'} if failed else {'INFO'}, summary)
        return {'FINISHED'}


class STACKBYTAG_OT_change_prefix(Operator):
    bl_idname = "nla.change_action_prefix_by_tag"
    bl_label = "Change Prefixes"
    bl_description = "Replace the recognized prefix in every matching action name"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.stack_by_tag_settings
        old_prefix = settings.prefix

        if not split_name_words(old_prefix):
            self.report({'WARNING'}, "Enter a Tag / Prefix before changing prefixes")
            return {'CANCELLED'}

        matching_actions = [
            action
            for action in bpy.data.actions
            if not action_is_empty(action)
            and action_name_matches_prefix(action.name, old_prefix)
        ]
        if not matching_actions:
            self.report({'WARNING'}, f"No non-empty actions match prefix: {old_prefix.strip()}")
            return {'CANCELLED'}

        renamed = 0
        unchanged = 0
        invalid = 0
        for action in matching_actions:
            new_name = replace_action_name_prefix(
                action.name,
                old_prefix,
                settings.replacement_prefix,
            )
            if new_name is None:
                invalid += 1
                continue
            if new_name == action.name:
                unchanged += 1
                continue

            action.name = new_name
            renamed += 1

        if not renamed:
            details = []
            if unchanged:
                details.append(f"{unchanged} unchanged")
            if invalid:
                details.append(f"{invalid} would have an empty name")
            suffix = f" ({', '.join(details)})" if details else ""
            self.report({'WARNING'}, "No action names changed" + suffix)
            return {'CANCELLED'}

        summary = f"Changed prefix on {renamed} action(s)"
        if unchanged or invalid:
            summary += f"; skipped {unchanged + invalid}"

        new_filter = settings.replacement_prefix.strip()
        if new_filter:
            settings.prefix = new_filter

        self.report({'INFO'}, summary)
        return {'FINISHED'}


class NLA_PT_stack_by_tag(Panel):
    bl_label = "Stack By Tag"
    bl_idname = "NLA_PT_stack_by_tag"
    bl_space_type = 'NLA_EDITOR'
    bl_region_type = 'UI'
    bl_category = "StackByTag"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.stack_by_tag_settings
        target = context.active_object

        target_box = layout.box()
        target_box.label(text="Target")
        if target is None:
            target_box.label(text="No active object", icon='ERROR')
        else:
            target_box.label(text=target.name, icon='OBJECT_DATA')

        layout.prop(settings, "prefix")
        layout.label(text="Blank matches all actions", icon='INFO')

        action_count = sum(
            1
            for action in bpy.data.actions
            if not action_is_empty(action)
            and action_name_matches_prefix(action.name, settings.prefix)
        )
        layout.label(text=f"{action_count} matching action(s)")

        rename_box = layout.box()
        rename_box.label(text="Rename Matching Actions")
        rename_box.prop(settings, "replacement_prefix")
        rename_column = rename_box.column()
        rename_column.enabled = bool(split_name_words(settings.prefix)) and action_count > 0
        rename_column.operator(STACKBYTAG_OT_change_prefix.bl_idname, icon='FILE_REFRESH')

        layout.separator()
        layout.prop(settings, "start_frame")
        layout.prop(settings, "skip_existing")

        column = layout.column()
        column.enabled = target is not None and target.is_editable
        column.operator(STACKBYTAG_OT_stack_actions.bl_idname, icon='NLA_PUSHDOWN')


classes = (
    STACKBYTAG_PG_settings,
    STACKBYTAG_OT_stack_actions,
    STACKBYTAG_OT_change_prefix,
    NLA_PT_stack_by_tag,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.stack_by_tag_settings = PointerProperty(type=STACKBYTAG_PG_settings)


def unregister():
    del bpy.types.Scene.stack_by_tag_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
