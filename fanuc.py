"""Simulación didáctica de posicionamiento articular del FANUC de cinco ejes."""

import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from modbus import ProtocolError, parse_address
from simulation import AddressField, check_addresses, configured, nonnegative_number, startup_error


# Semirrecorridos en coordenadas centradas del simulador y velocidades de ficha.
AXES = (("J1", 180, 450), ("J2", 122.5, 380), ("J3", 210, 520),
        ("J4", 125, 545), ("J5", 360, 1500))
COMMANDS = (("enable", "Habilitar"), ("execute", "Ejecutar (flanco)"),
            ("stop", "Paro"), ("grip", "Cerrar pinza"))
STATES = (("ready", "Listo"), ("moving", "En movimiento"),
          ("done", "Movimiento terminado"), ("closed", "Pinza cerrada"),
          ("fault", "Consigna fuera de rango"))
READ_KEYS = (*[key for key, _ in COMMANDS], *[f"in_{axis}" for axis, _, _ in AXES])
WRITE_KEYS = (*[key for key, _ in STATES], *[f"out_{axis}" for axis, _, _ in AXES])
LINKS = (100, 97, 44)
SHOULDER_HEIGHT = 46
VIEWS = ("Vista 3D", "Lateral · XZ", "Frontal · YZ", "Superior · XY")


def decode_angles(words):
    return tuple((value if value < 32768 else value - 65536) / 10 for value in words)


class Motion:
    def __init__(self):
        self.position = (0.0,) * len(AXES)
        self.goal = None
        self.done = False
        self.fault = False

    def command(self, goal, speed):
        self.stop()
        self.fault = any(abs(angle) > limit for angle, (_, limit, _) in zip(goal, AXES))
        if self.fault:
            return
        self.start = self.position
        self.duration = max(abs(end - start) / (rate * speed)
                            for start, end, (_, _, rate) in zip(self.start, goal, AXES))
        self.elapsed = 0.0
        self.goal = goal
        self.advance(0)

    def advance(self, dt):
        if self.goal is None:
            return
        self.elapsed += dt
        if self.elapsed >= self.duration:
            self.position = self.goal
            self.goal = None
            self.done = True
            return
        fraction = self.elapsed / self.duration
        self.position = tuple(start + (end - start) * fraction for start, end in zip(self.start, self.goal))

    def stop(self):
        self.goal = None
        self.done = False


class FanucSimulator:
    def __init__(self, parent, endpoint, exchange, save, compatibility):
        self.parent, self.endpoint, self.exchange = parent, endpoint, exchange
        self.save, self.compatibility = save, compatibility
        self.fields, self.config_controls = {}, []
        self.cancel = self.timer = None
        self.results = queue.Queue()
        self.pending = False
        self.motion = Motion()
        self.closed = self.ready = False
        self.status = tk.StringVar(value="En pausa")
        self.display = [tk.StringVar(value="0,0°") for _ in AXES]
        self.manual_grip = tk.BooleanVar()
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x")
        self.start_button = ttk.Button(toolbar, text="Iniciar")
        self.start_button.pack(side="left")
        ttk.Label(toolbar, textvariable=self.status).pack(side="left", padx=10)
        self.fields["workspace"] = tk.StringVar(value="1")
        ttk.Checkbutton(toolbar, text="Zona de trabajo", variable=self.fields["workspace"],
                        onvalue="1", offvalue="0", command=self.toggle_workspace).pack(side="right")
        self.canvas = tk.Canvas(parent, height=410, background="#eaf0f4", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=6)
        self.canvas.bind("<Configure>", lambda event: self.draw())

        panels = ttk.Frame(parent)
        panels.pack(fill="x")
        joints = ttk.LabelFrame(panels, text="Ejes · palabras INT en 0,1°", padding=8)
        joints.pack(side="left", fill="both", expand=True, padx=(0, 8))
        joints.columnconfigure(2, weight=1)
        self.sliders = []
        for column, heading in enumerate(("Eje", "Objetivo (°)", "Ajuste manual", "%MW objetivo", "%MW actual", "Actual")):
            ttk.Label(joints, text=heading).grid(row=0, column=column, padx=5)
        for index, (axis, limit, _) in enumerate(AXES):
            row = index + 1
            ttk.Label(joints, text=f"{axis}   ±{limit:g}°").grid(row=row, column=0, sticky="w")
            self.field(joints, row, 1, f"manual_{axis}", "0", config=False)
            target = self.fields[f"manual_{axis}"]
            slider_value = tk.DoubleVar(value=0)
            slider = ttk.Scale(joints, from_=-limit, to=limit, variable=slider_value, length=110,
                               command=lambda value, target=target: target.set(f"{float(value):.1f}"))
            slider.grid(row=row, column=2, sticky="ew", padx=5)
            self.sliders.append(slider)
            target.trace_add("write", lambda *_, target=target, slider_value=slider_value, limit=limit:
                             self.sync_slider(target, slider_value, limit))
            slider.bind("<ButtonRelease-1>", lambda event: self.save())
            slider.bind("<KeyRelease>", lambda event: self.save())
            self.field(joints, row, 3, f"in_{axis}")
            self.field(joints, row, 4, f"out_{axis}")
            ttk.Label(joints, textvariable=self.display[index], width=9).grid(row=row, column=5)
        ttk.Label(joints, text="Origen").grid(row=6, column=0, sticky="w")
        self.field(joints, 6, 1, "source", "Manual", ("Manual", "PLC"))
        ttk.Label(joints, text="Velocidad %").grid(row=7, column=0, sticky="w")
        self.field(joints, 7, 1, "speed", "15")
        buttons = ttk.Frame(joints)
        buttons.grid(row=8, column=0, columnspan=6, sticky="w", pady=(5, 0))
        self.manual_controls = []
        for label, action in (("Mover", self.manual_move), ("Posición cero", lambda: self.manual_move(True)),
                              ("Parar", lambda: self.motion.stop())):
            button = ttk.Button(buttons, text=label, command=action, state="disabled")
            button.pack(side="left", padx=(0, 6))
            self.manual_controls.append(button)
        grip = ttk.Checkbutton(buttons, text="Pinza", variable=self.manual_grip, state="disabled")
        grip.pack(side="left")
        self.manual_controls.append(grip)
        for title, entries in (("Mandos de bit", COMMANDS), ("Estados de bit", STATES)):
            card = ttk.LabelFrame(panels, text=title, padding=8)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            for row, (key, label) in enumerate(entries):
                ttk.Label(card, text=label).grid(row=row, column=0, sticky="w", padx=(0, 5))
                self.field(card, row, 1, key)

    @staticmethod
    def sync_slider(target, slider_value, limit):
        try:
            value = float(target.get().replace(",", "."))
        except ValueError:
            return
        if math.isfinite(value):
            slider_value.set(max(-limit, min(limit, value)))

    def toggle_workspace(self):
        self.draw()
        self.save()

    def field(self, panel, row, column, key, default="", choices=(), config=True):
        var = self.fields[key] = tk.StringVar(value=default)
        widget = (ttk.Combobox(panel, textvariable=var, values=choices, state="readonly", width=11)
                  if choices else ttk.Entry(panel, textvariable=var, width=13))
        widget.grid(row=row, column=column, sticky="ew", padx=4, pady=2)
        widget.bind("<FocusOut>", lambda event: self.save())
        widget.bind("<<ComboboxSelected>>", lambda event: self.save())
        if config:
            self.config_controls.append(widget)

    def load(self, settings):
        for key, value in settings.items():
            if key in self.fields:
                self.fields[key].set(value)

    def reset_session(self):
        self.pause()
        self.motion = Motion()
        self.manual_grip.set(False)
        self.closed = False
        self.draw()

    def addresses(self):
        specs = [(key, title, False) for key, title in (*COMMANDS, *STATES)]
        specs += [(f"{prefix}_{axis}", f"{axis} · {label}", True)
                  for axis, _, _ in AXES for prefix, label in (("in", "Objetivo"), ("out", "Actual"))]
        fields = {}
        for key, label, word in specs:
            raw = self.fields[key].get().strip()
            title = f"FANUC / {label}"
            try:
                address = parse_address(raw) if raw else None
                if address is None and (key in ("enable", "execute") or key.startswith("in_")):
                    raise ValueError("Asigna una dirección para el modo PLC.")
                if address is not None and word == (address.coil or address.bit is not None):
                    raise ValueError("Se requiere %MW completa." if word else "Se requiere un bit.")
            except ValueError as error:
                raise ValueError(f"{configured(title, raw)}. {error}") from None
            fields[key] = AddressField(title, raw, address)
        check_addresses(fields.values())
        bits = [field for field in fields.values() if field.address and field.address.bit is not None]
        words = [field for field in fields.values()
                 if field.address and not field.address.coil and field.address.bit is None]
        check_addresses(words, bits, shared_word=True)
        return {key: field.address for key, field in fields.items()}

    def toggle(self):
        if self.cancel is not None:
            self.pause()
            return
        if self.pending:
            self.status.set("Esperando que termine la consulta anterior.")
            return
        try:
            speed = nonnegative_number(self.fields["speed"].get(), "FANUC / Velocidad %")
            if not 0 < speed <= 100:
                raise ValueError("FANUC / Velocidad %. Introduce un valor mayor que 0 y hasta 100.")
            self.target = self.endpoint() if self.fields["source"].get() == "PLC" else None
            self.mapping = self.addresses() if self.target else {}
        except ValueError as error:
            startup_error(self.parent, "FANUC", self.status, error)
            return
        self.save()
        self.speed = speed / 100
        self.cancel = threading.Event()
        self.previous_execute = None
        self.ready = self.target is None
        self.motion.fault = False
        self.mask_fallback = self.compatibility.get()
        self.last_tick = self.next_sample = time.monotonic()
        self.start_button.configure(text="Pausar")
        self.set_controls(True)
        self.status.set("Mando manual" if self.target is None else "Leyendo entradas…")
        self.tick()

    def set_controls(self, running):
        for widget in self.config_controls:
            widget.configure(state=("disabled" if running else
                                    "readonly" if isinstance(widget, ttk.Combobox) else "normal"))
        for widget in self.manual_controls:
            widget.configure(state="normal" if running and self.target is None else "disabled")
        for slider in self.sliders:
            slider.configure(state="disabled" if running and self.target is not None else "normal")

    def pause(self):
        if self.cancel is None:
            return
        self.cancel.set()
        self.cancel = None
        if self.timer is not None:
            self.parent.after_cancel(self.timer)
            self.timer = None
        self.motion.stop()
        self.ready = False
        self.start_button.configure(text="Iniciar")
        self.set_controls(False)
        self.status.set("En pausa")
        self.draw()

    def manual_move(self, zero=False):
        try:
            values = ((0.0,) * len(AXES) if zero else
                      tuple(float(self.fields[f"manual_{axis}"].get().replace(",", ".")) for axis, _, _ in AXES))
            if not all(math.isfinite(value) for value in values):
                raise ValueError
        except ValueError:
            self.status.set("Introduce un ángulo válido en cada eje.")
            return
        self.motion.command(values, self.speed)
        self.status.set("Consigna fuera de rango" if self.motion.fault else "Mando manual")

    def tick(self):
        now = time.monotonic()
        self.motion.advance(min(0.2, now - self.last_tick))
        self.last_tick = now
        if self.target is None:
            self.closed = self.manual_grip.get()
        elif not self.pending and now >= self.next_sample:
            values = (self.ready, self.motion.goal is not None, self.motion.done, self.closed, self.motion.fault,
                      *(round(angle * 10) & 0xFFFF for angle in self.motion.position))
            self.pending = True
            cancel = self.cancel
            threading.Thread(target=self.exchange_cycle, daemon=True, args=(
                cancel, self.target, self.mapping, self.mask_fallback, values,
            )).start()
            self.parent.after(20, self.poll, cancel)
        self.draw()
        self.timer = self.parent.after(40, self.tick)

    def exchange_cycle(self, cancel, target, mapping, compatibility, values):
        try:
            readings = [self.exchange(*target, mapping[key], cancelled=cancel) if mapping[key] else 0
                        for key in READ_KEYS]
            for key, value in zip(WRITE_KEYS, values):
                if mapping[key] and not cancel.is_set():
                    self.exchange(*target, mapping[key], int(value), compatibility, cancelled=cancel)
        except (OSError, ProtocolError) as error:
            self.results.put((None, str(error)))
        else:
            self.results.put((readings, None))

    def poll(self, cancel):
        try:
            readings, error = self.results.get_nowait()
        except queue.Empty:
            self.parent.after(20, self.poll, cancel)
            return
        self.pending = False
        if self.cancel is not cancel:
            return
        self.next_sample = time.monotonic() + 0.10
        if error:
            self.motion.stop()
            self.ready = False
            self.previous_execute = None
            self.status.set(f"Error Modbus · {error}")
            return
        enable, execute, stop, grip, *angles = readings
        self.ready = bool(enable and not stop)
        self.closed = bool(grip)
        if not self.ready:
            self.motion.stop()
        elif execute and self.previous_execute == 0:
            self.motion.command(decode_angles(angles), self.speed)
        self.previous_execute = execute
        source = "Memoria local" if self.target[0] is None else "PLC conectado"
        state = "Consigna fuera de rango" if self.motion.fault else "Habilitado" if self.ready else "Parado"
        self.status.set(f"{source} · {state}")

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 60 or height < 60:
            return
        for var, angle in zip(self.display, self.motion.position):
            var.set(f"{angle:.1f}°")
        points, fingers = self.geometry()
        card_width, card_height = (width - 18) / 2, (height - 18) / 2
        for index, title in enumerate(VIEWS):
            x = 6 + index % 2 * (card_width + 6)
            y = 6 + index // 2 * (card_height + 6)
            self.draw_view(index, title, x, y, card_width, card_height, points, fingers)

    def geometry(self):
        yaw, shoulder, elbow, wrist, roll = map(math.radians, self.motion.position)
        cy, sy = math.cos(yaw), math.sin(yaw)
        radial, z = 0.0, SHOULDER_HEIGHT
        points = [(0, 0, 0), (0, 0, z)]
        first = math.pi / 2 - shoulder
        second = first - 1.10 - elbow
        last = second - 0.60 - wrist
        for length, angle in zip(LINKS, (first, second, last)):
            radial += length * math.cos(angle)
            z += length * math.sin(angle)
            points.append((radial * cy, radial * sy, z))
        axis = math.cos(last) * cy, math.cos(last) * sy, math.sin(last)
        tangent = -sy, cy, 0
        normal = (-math.sin(last) * cy, -math.sin(last) * sy, math.cos(last))
        gap = 6 if self.closed else 16
        offset = tuple(gap * (math.cos(roll) * a + math.sin(roll) * b) for a, b in zip(tangent, normal))
        fingers = []
        for sign in (-1, 1):
            edge = tuple(a + sign * b for a, b in zip(points[-1], offset))
            fingers.append((edge, tuple(a + 22 * b for a, b in zip(edge, axis))))
        return points, fingers

    @staticmethod
    def projection(index, point):
        x, y, z = point
        if index == 0:
            return (x - y) * math.sqrt(0.5), (x + y - 2 * z) / math.sqrt(6)
        if index == 1:
            return x, -z
        if index == 2:
            return y, -z
        return x, -y

    def draw_view(self, index, title, left, top, width, height, points, fingers):
        canvas = self.canvas
        canvas.create_rectangle(left, top, left + width, top + height, fill="#f7fafc", outline="#bfd0db")
        px, py = self.projection(index, (0, 0, SHOULDER_HEIGHT))
        scale = min((width - 12) / 555, max(0.01, (height - 28) / 555))
        cx, cy = left + width / 2, top + (height + 22) / 2

        def project(point):
            x, y = self.projection(index, point)
            return cx + (x - px) * scale, cy + (y - py) * scale

        if self.fields["workspace"].get() == "1":
            radius = sum(LINKS) * scale
            canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                               outline="#5a92ae", fill="#e8f3f8", dash=(4, 4), tags="workspace")
        projected = [project(point) for point in points]
        bx, by = projected[0]
        base_x, base_y = 33 * scale, (33 if index == 3 else 13) * scale
        canvas.create_oval(bx - base_x, by - base_y, bx + base_x, by + base_y, fill="#425461", outline="#293b46")
        for a, b in zip(projected, projected[1:]):
            canvas.create_line(*a, *b, fill="#283c47", width=max(2, round(27*scale)), capstyle="round")
            canvas.create_line(*a, *b, fill="#f4be32", width=max(2, round(21*scale)), capstyle="round")
        for x, y in projected[1:]:
            r = max(2, 10 * scale)
            canvas.create_oval(x-r, y-r, x+r, y+r, fill="#546675", outline="#243944", width=2)
        for edge, tip in fingers:
            canvas.create_line(*projected[-1], *project(edge), *project(tip), fill="#233e50", width=2)
        canvas.create_text(left + 8, top + 6, anchor="nw", text=title,
                           fill="#18364a", font=("Segoe UI", 10, "bold"), tags="view_title")
        if index == 0:
            state = "En movimiento" if self.motion.goal else "Posición alcanzada" if self.motion.done else "Parado"
            canvas.create_text(left + width - 8, top + 6, anchor="ne", text=state, fill="#18364a")
