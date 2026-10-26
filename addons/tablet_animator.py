bl_info = {
    "name": "Tablet Organic Animator",
    "author": "Gemini & Ty",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Ctrl+Shift+T",
    "description": "Animacja tabletem: Ruch (Lazy) + Skala (Nacisk)",
    "category": "Animation",
}

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

# --- OPERATOR GŁÓWNY ---
class ModalTabletAnimator(bpy.types.Operator):
    """Start Tablet Animation (Move + Pressure Scale)"""
    bl_idname = "object.tablet_organic_animator"
    bl_label = "Tablet Organic Animator"
    bl_options = {'REGISTER', 'UNDO'}

    # --- KONFIGURACJA ADD-ONU ---
    smooth_move = 0.1        # Wygładzanie ruchu (Lazy Mouse)
    scale_strength = 2.0     # Maksymalne powiększenie
    smooth_scale = 0.1       # Wygładzanie skali
    pressure_curve = 2.5     # Krzywa nacisku (2.5 = miękki start)

    def modal(self, context, event):
        obj = context.object

        # 1. WYJŚCIE (ESC)
        if event.type == 'ESC':
            self.report({'INFO'}, "Koniec animacji tabletem")
            return {'FINISHED'}

        # 2. GŁÓWNA LOGIKA
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            
            # --- RUCH (FIXED SYNC) ---
            region = context.region
            rv3d = context.region_data
            mouse_pos = (event.mouse_region_x, event.mouse_region_y)

            # Rzutowanie na płaszczyznę obiektu
            view_vector = view3d_utils.region_2d_to_location_3d(region, rv3d, mouse_pos, self.fixed_depth_location)
            target_location = view_vector + self.offset_vec

            # Interpolacja ruchu (Lerp)
            obj.location = obj.location.lerp(target_location, self.smooth_move)

            # --- SKALA (KRZYWA WYKŁADNICZA) ---
            try:
                # Pobieramy nacisk (jeśli brak, to 0)
                raw_pressure = event.pressure if event.pressure > 0 else 0
            except AttributeError:
                raw_pressure = 0

            # Zastosowanie krzywej (potęgowanie) dla organicznego czucia
            curved_pressure = raw_pressure ** self.pressure_curve

            target_scale_factor = 1.0 + (curved_pressure * self.scale_strength)
            target_scale_vec = self.init_scale * target_scale_factor
            
            # Interpolacja skali
            obj.scale = obj.scale.lerp(target_scale_vec, self.smooth_scale)

            # --- AUTO KEYING (NAGRYWANIE) ---
            if context.scene.tool_settings.use_keyframe_insert_auto:
                obj.keyframe_insert(data_path="location")
                obj.keyframe_insert(data_path="scale")

            return {'RUNNING_MODAL'}

        # Blokada kliknięć (żeby dotyk nie psuł skryptu)
        if event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'}:
            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        if context.object and context.area.type == 'VIEW_3D':
            obj = context.object
            region = context.region
            rv3d = context.region_data
            
            # Zapamiętujemy głębokość
            self.fixed_depth_location = obj.location.copy()
            
            # Obliczamy offset startowy
            mouse_pos = (event.mouse_region_x, event.mouse_region_y)
            start_ray = view3d_utils.region_2d_to_location_3d(region, rv3d, mouse_pos, self.fixed_depth_location)
            self.offset_vec = obj.location - start_ray

            # Zapamiętujemy skalę bazową
            self.init_scale = obj.scale.copy()
            
            self.report({'INFO'}, "Tablet Start! (ESC by wyjść)")
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        else:
            self.report({'WARNING'}, "Wybierz obiekt i najedź na widok 3D!")
            return {'CANCELLED'}

# --- REJESTRACJA I SKRÓTY KLAWISZOWE ---

addon_keymaps = []

def register():
    bpy.utils.register_class(ModalTabletAnimator)
    
    # Dodawanie skrótu klawiszowego
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
        # USTAWIENIE SKRÓTU: CTRL + SHIFT + T
        kmi = km.keymap_items.new(ModalTabletAnimator.bl_idname, 'T', 'PRESS', ctrl=True, shift=True)
        addon_keymaps.append((km, kmi))

def unregister():
    # Usuwanie skrótu klawiszowego
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    
    bpy.utils.unregister_class(ModalTabletAnimator)

if __name__ == "__main__":
    register()