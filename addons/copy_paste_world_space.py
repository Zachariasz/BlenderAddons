bl_info = {
    "name": "Copy & Paste World Space",
    "author": "Antigravity (Local Qwen Coder)",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "3D View > Copy Attributes Menu (Ctrl+C), Context Menus",
    "description": "Copy and paste world space location and rotation between Objects and Pose Bones. Integrates seamlessly into the Ctrl+C Copy Attributes Menu.",
    "category": "Animation",
}

import bpy
from mathutils import Matrix, Vector, Quaternion

# Global in-memory clipboard for World Space transforms
_WS_CLIPBOARD = {
    "valid": False,
    "location": Vector((0.0, 0.0, 0.0)),
    "rotation": Quaternion((1.0, 0.0, 0.0, 0.0)),
    "scale": Vector((1.0, 1.0, 1.0)),
    "matrix": Matrix.Identity(4),
    "name": "",
    "type": "",
}


def get_active_world_matrix(context):
    """
    Returns (world_matrix, display_name, item_type) for the active Pose Bone or Object.
    """
    if context.mode == 'POSE' and context.active_pose_bone:
        pb = context.active_pose_bone
        arm = pb.id_data
        world_matrix = (arm.matrix_world @ pb.matrix).copy()
        return world_matrix, f"{pb.name}", "POSE BONE"
    
    if context.active_object:
        obj = context.active_object
        world_matrix = obj.matrix_world.copy()
        return world_matrix, obj.name, "OBJECT"
        
    return None, None, None


class VIEW3D_OT_copy_world_space(bpy.types.Operator):
    """Copy world space position and rotation from the active Object or Pose Bone"""
    bl_idname = "view3d.copy_world_space"
    bl_label = "Copy World Space"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if context.mode == 'POSE' and context.active_pose_bone:
            return True
        return context.active_object is not None

    def execute(self, context):
        world_matrix, display_name, item_type = get_active_world_matrix(context)
        if not world_matrix:
            self.report({'WARNING'}, "No active Object or Pose Bone found to copy.")
            return {'CANCELLED'}

        loc, rot, scale = world_matrix.decompose()

        _WS_CLIPBOARD["valid"] = True
        _WS_CLIPBOARD["location"] = loc.copy()
        _WS_CLIPBOARD["rotation"] = rot.copy()
        _WS_CLIPBOARD["scale"] = scale.copy()
        _WS_CLIPBOARD["matrix"] = world_matrix.copy()
        _WS_CLIPBOARD["name"] = display_name
        _WS_CLIPBOARD["type"] = item_type

        self.report({'INFO'}, f"Copied World Space of '{display_name}' ({item_type})")
        return {'FINISHED'}


class VIEW3D_OT_paste_world_space(bpy.types.Operator):
    """Paste copied world space position and rotation to selected Object(s) or Pose Bone(s)"""
    bl_idname = "view3d.paste_world_space"
    bl_label = "Paste World Space"
    bl_options = {'REGISTER', 'UNDO'}

    paste_location: bpy.props.BoolProperty(
        name="Location",
        description="Paste world space location",
        default=True,
    )
    paste_rotation: bpy.props.BoolProperty(
        name="Rotation",
        description="Paste world space rotation",
        default=True,
    )
    paste_scale: bpy.props.BoolProperty(
        name="Scale",
        description="Paste scale (defaults to preserving current scale)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        if not _WS_CLIPBOARD.get("valid", False):
            return False
        if context.mode == 'POSE':
            return bool(context.selected_pose_bones or context.active_pose_bone)
        return bool(context.selected_objects or context.active_object)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Transform Channels:", icon='CON_LOCLIKE')
        row = box.row(align=True)
        row.prop(self, "paste_location", toggle=True, icon='AXIS_SIDE')
        row.prop(self, "paste_rotation", toggle=True, icon='DRIVER_ROTATIONAL_DIFFERENCE')
        row.prop(self, "paste_scale", toggle=True, icon='FULLSCREEN_ENTER')

    def execute(self, context):
        if not _WS_CLIPBOARD.get("valid", False):
            self.report({'WARNING'}, "Clipboard is empty. Copy world space first.")
            return {'CANCELLED'}

        clip_loc = _WS_CLIPBOARD["location"]
        clip_rot = _WS_CLIPBOARD["rotation"]
        clip_scale = _WS_CLIPBOARD["scale"]
        src_name = _WS_CLIPBOARD.get("name", "Clipboard")

        auto_key = context.scene.tool_settings.use_keyframe_insert_auto

        if context.mode == 'POSE':
            targets = context.selected_pose_bones if context.selected_pose_bones else ([context.active_pose_bone] if context.active_pose_bone else [])
            if not targets:
                self.report({'WARNING'}, "No pose bone selected to paste onto.")
                return {'CANCELLED'}

            for pb in targets:
                arm = pb.id_data
                curr_world = arm.matrix_world @ pb.matrix
                curr_loc, curr_rot, curr_scale = curr_world.decompose()

                final_loc = clip_loc if self.paste_location else curr_loc
                final_rot = clip_rot if self.paste_rotation else curr_rot
                final_scale = clip_scale if self.paste_scale else curr_scale

                final_world_mat = Matrix.LocRotScale(final_loc, final_rot, final_scale)
                pb.matrix = arm.matrix_world.inverted() @ final_world_mat

                if auto_key:
                    if self.paste_location:
                        pb.keyframe_insert(data_path="location")
                    if self.paste_rotation:
                        if pb.rotation_mode == 'QUATERNION':
                            pb.keyframe_insert(data_path="rotation_quaternion")
                        elif pb.rotation_mode == 'AXIS_ANGLE':
                            pb.keyframe_insert(data_path="rotation_axis_angle")
                        else:
                            pb.keyframe_insert(data_path="rotation_euler")
                    if self.paste_scale:
                        pb.keyframe_insert(data_path="scale")

            context.view_layer.update()
            self.report({'INFO'}, f"Pasted World Space onto {len(targets)} bone(s) from '{src_name}'")
            return {'FINISHED'}

        else:
            targets = context.selected_objects if context.selected_objects else ([context.active_object] if context.active_object else [])
            if not targets:
                self.report({'WARNING'}, "No object selected to paste onto.")
                return {'CANCELLED'}

            for obj in targets:
                curr_loc, curr_rot, curr_scale = obj.matrix_world.decompose()

                final_loc = clip_loc if self.paste_location else curr_loc
                final_rot = clip_rot if self.paste_rotation else curr_rot
                final_scale = clip_scale if self.paste_scale else curr_scale

                final_world_mat = Matrix.LocRotScale(final_loc, final_rot, final_scale)
                obj.matrix_world = final_world_mat

                if auto_key:
                    if self.paste_location:
                        obj.keyframe_insert(data_path="location")
                    if self.paste_rotation:
                        if obj.rotation_mode == 'QUATERNION':
                            obj.keyframe_insert(data_path="rotation_quaternion")
                        elif obj.rotation_mode == 'AXIS_ANGLE':
                            obj.keyframe_insert(data_path="rotation_axis_angle")
                        else:
                            obj.keyframe_insert(data_path="rotation_euler")
                    if self.paste_scale:
                        obj.keyframe_insert(data_path="scale")

            context.view_layer.update()
            self.report({'INFO'}, f"Pasted World Space onto {len(targets)} object(s) from '{src_name}'")
            return {'FINISHED'}


def draw_world_space_menu_items(self, context):
    """Draw menu entries inside Copy Attributes popup and context menus"""
    layout = self.layout
    layout.separator()

    has_clip = _WS_CLIPBOARD.get("valid", False)
    src_name = _WS_CLIPBOARD.get("name", "")

    # Identify active target name for the label
    active_target = ""
    if context.mode == 'POSE' and context.active_pose_bone:
        active_target = f" ({context.active_pose_bone.name})"
    elif context.active_object:
        active_target = f" ({context.active_object.name})"

    layout.label(text="World Space:", icon='WORLD')

    row_copy = layout.row()
    row_copy.operator(
        "view3d.copy_world_space",
        text=f"Copy World Space{active_target}",
        icon='COPYDOWN',
    )

    paste_label = f"Paste World Space (from {src_name})" if (has_clip and src_name) else "Paste World Space"
    row_paste = layout.row()
    row_paste.enabled = has_clip
    row_paste.operator(
        "view3d.paste_world_space",
        text=paste_label,
        icon='PASTEDOWN',
    )


class WS_PT_preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    add_to_context_menu: bpy.props.BoolProperty(
        name="Show in Right-Click Context Menu",
        description="Also add Copy/Paste World Space to 3D View right-click context menu",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "add_to_context_menu")
        has_clip = _WS_CLIPBOARD.get("valid", False)
        if has_clip:
            layout.separator()
            box = layout.box()
            box.label(text=f"Current Clipboard: '{_WS_CLIPBOARD['name']}' ({_WS_CLIPBOARD['type']})", icon='INFO')
            loc = _WS_CLIPBOARD['location']
            box.label(text=f"Location: ({loc.x:.3f}, {loc.y:.3f}, {loc.z:.3f})")


classes = (
    VIEW3D_OT_copy_world_space,
    VIEW3D_OT_paste_world_space,
    WS_PT_preferences,
)

_ATTACHED_MENUS = []

def attach_to_menus():
    """Safely attach to Copy Attributes menus and context menus"""
    global _ATTACHED_MENUS

    # Target menu types:
    # VIEW3D_MT_copypopup: Object Mode Ctrl+C Copy Attributes Menu
    # VIEW3D_MT_posecopypopup: Pose Mode Ctrl+C Copy Attributes Menu
    # VIEW3D_MT_object_context_menu: Object Mode Right Click Menu
    # VIEW3D_MT_pose_context_menu: Pose Mode Right Click Menu
    menus_to_hook = [
        "VIEW3D_MT_copypopup",
        "VIEW3D_MT_posecopypopup",
        "VIEW3D_MT_object_context_menu",
        "VIEW3D_MT_pose_context_menu",
    ]

    for menu_name in menus_to_hook:
        menu_cls = getattr(bpy.types, menu_name, None)
        if menu_cls and menu_cls not in _ATTACHED_MENUS:
            try:
                menu_cls.remove(draw_world_space_menu_items)
            except ValueError:
                pass
            menu_cls.append(draw_world_space_menu_items)
            _ATTACHED_MENUS.append(menu_cls)


def detach_from_menus():
    """Safely detach from all menus"""
    global _ATTACHED_MENUS
    for menu_cls in _ATTACHED_MENUS:
        try:
            menu_cls.remove(draw_world_space_menu_items)
        except ValueError:
            pass
    _ATTACHED_MENUS.clear()


def deferred_menu_attach():
    """Timer callback to attach if Copy Attributes Menu was registered later"""
    attach_to_menus()
    return None  # One-shot timer


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    attach_to_menus()

    # Schedule a brief timer to guarantee attachment even if other addons load after
    if not bpy.app.timers.is_registered(deferred_menu_attach):
        bpy.app.timers.register(deferred_menu_attach, first_interval=0.2)


def unregister():
    if bpy.app.timers.is_registered(deferred_menu_attach):
        bpy.app.timers.unregister(deferred_menu_attach)

    detach_from_menus()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
