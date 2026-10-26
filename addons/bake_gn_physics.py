# -*- coding: utf-8 -*-
bl_info = {
    "name": "Bake Selected Geometry Nodes Physics",
    "author": "Antigravity",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N-Panel) > GN Bake",
    "description": "Bakes all Geometry Nodes simulation and bake nodes on all selected objects",
    "warning": "",
    "doc_url": "",
    "category": "Animation",
}

import bpy


def restore_selection(orig_active, orig_selected):
    """Safely restore previous active and selected objects in the view layer."""
    try:
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        for obj in orig_selected:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        if orig_active and orig_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = orig_active
    except Exception as e:
        print(f"[GN Bake] Warning restoring selection: {e}")


class OBJECT_OT_bake_all_gn_physics(bpy.types.Operator):
    """Bake all Geometry Nodes simulation/physics bake items across selected objects"""
    bl_idname = "object.bake_all_gn_physics"
    bl_label = "Bake All Selected GN Physics"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        orig_active = context.view_layer.objects.active
        orig_selected = list(context.selected_objects)
        
        baked_count = 0
        objects_processed = 0

        print("\n" + "=" * 50)
        print("[GN Bake] Starting Geometry Nodes Physics Bake...")

        try:
            for obj in orig_selected:
                if not obj or obj.type != 'MESH':
                    continue

                obj_had_bake = False
                for mod in obj.modifiers:
                    if mod.type != 'NODES' or not hasattr(mod, "bakes"):
                        continue

                    for bake in mod.bakes:
                        context.view_layer.objects.active = obj
                        obj.select_set(True)

                        bake_name = getattr(bake, "name", "Bake Item")
                        node_name = getattr(getattr(bake, "node", None), "name", "Bake Node")
                        print(f"[GN Bake] Baking object '{obj.name}' -> Modifier '{mod.name}' -> {bake_name} ({node_name})")

                        if getattr(bake, "is_baked", False):
                            try:
                                bpy.ops.object.geometry_node_bake_delete_single(
                                    session_uid=obj.id_data.session_uid,
                                    modifier_name=mod.name,
                                    bake_id=bake.bake_id,
                                )
                                print(f"[GN Bake] Cleared existing cache for '{bake_name}'")
                            except Exception as del_err:
                                print(f"[GN Bake] Note on cache clear: {del_err}")

                        try:
                            bpy.ops.object.geometry_node_bake_single(
                                session_uid=obj.id_data.session_uid,
                                modifier_name=mod.name,
                                bake_id=bake.bake_id,
                            )
                            baked_count += 1
                            obj_had_bake = True
                        except Exception as bake_err:
                            msg = f"Failed baking '{obj.name}' ({mod.name}): {bake_err}"
                            print(f"[GN Bake ERROR] {msg}")
                            self.report({'WARNING'}, msg)

                if obj_had_bake:
                    objects_processed += 1

        finally:
            restore_selection(orig_active, orig_selected)

        print(f"[GN Bake] Finished! Total bake items processed: {baked_count} across {objects_processed} object(s).")
        print("=" * 50 + "\n")

        if baked_count > 0:
            self.report({'INFO'}, f"Successfully baked {baked_count} GN bake item(s) on {objects_processed} object(s).")
        else:
            self.report({'WARNING'}, "No Geometry Nodes simulation/bake nodes found on selected objects.")

        return {'FINISHED'}


class OBJECT_OT_delete_all_gn_physics(bpy.types.Operator):
    """Delete/Clear all Geometry Nodes bake caches across selected objects"""
    bl_idname = "object.delete_all_gn_physics"
    bl_label = "Delete All Selected GN Bakes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        orig_active = context.view_layer.objects.active
        orig_selected = list(context.selected_objects)
        
        deleted_count = 0
        objects_processed = 0

        print("\n" + "=" * 50)
        print("[GN Bake] Deleting Geometry Nodes Bake Caches...")

        try:
            for obj in orig_selected:
                if not obj or obj.type != 'MESH':
                    continue

                obj_had_delete = False
                for mod in obj.modifiers:
                    if mod.type != 'NODES' or not hasattr(mod, "bakes"):
                        continue

                    for bake in mod.bakes:
                        context.view_layer.objects.active = obj
                        obj.select_set(True)

                        try:
                            bpy.ops.object.geometry_node_bake_delete_single(
                                session_uid=obj.id_data.session_uid,
                                modifier_name=mod.name,
                                bake_id=bake.bake_id,
                            )
                            deleted_count += 1
                            obj_had_delete = True
                        except Exception as del_err:
                            print(f"[GN Bake] Delete note for {obj.name}: {del_err}")

                if obj_had_delete:
                    objects_processed += 1

        finally:
            restore_selection(orig_active, orig_selected)

        print(f"[GN Bake] Deleted {deleted_count} bake cache(s) across {objects_processed} object(s).")
        print("=" * 50 + "\n")

        if deleted_count > 0:
            self.report({'INFO'}, f"Deleted {deleted_count} GN bake cache(s) on {objects_processed} object(s).")
        else:
            self.report({'INFO'}, "No active GN bake caches found to delete.")

        return {'FINISHED'}


class VIEW3D_PT_gn_physics_bake(bpy.types.Panel):
    """3D Viewport Sidebar Panel for Geometry Nodes Physics Baking"""
    bl_label = "GN Physics Bake"
    bl_idname = "VIEW3D_PT_gn_physics_bake"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GN Bake'

    def draw(self, context):
        layout = self.layout
        selected = context.selected_objects

        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("object.bake_all_gn_physics", text="Bake All GN Physics", icon='PHYSICS')

        col_del = layout.column(align=True)
        col_del.scale_y = 1.1
        col_del.operator("object.delete_all_gn_physics", text="Delete All GN Bakes", icon='TRASH')

        # Info Box
        box = layout.box()
        gn_objects_count = 0
        total_bake_nodes = 0

        for obj in selected:
            if not obj or obj.type != 'MESH':
                continue
            has_bakes = False
            for mod in obj.modifiers:
                if mod.type == 'NODES' and hasattr(mod, "bakes"):
                    b_count = len(mod.bakes)
                    if b_count > 0:
                        total_bake_nodes += b_count
                        has_bakes = True
            if has_bakes:
                gn_objects_count += 1

        box.label(text=f"Selected Objects: {len(selected)}", icon='OBJECT_DATA')
        box.label(text=f"Objects w/ GN Bakes: {gn_objects_count}", icon='NODETREE')
        box.label(text=f"Total Bake Nodes: {total_bake_nodes}", icon='TIME')


classes = (
    OBJECT_OT_bake_all_gn_physics,
    OBJECT_OT_delete_all_gn_physics,
    VIEW3D_PT_gn_physics_bake,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()