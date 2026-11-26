"""Variador ATV320 simulado con control de dos hilos y cinta de botellas."""

import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from modbus import ProtocolError, parse_address
from simulation import AddressField, check_addresses, configured, nonnegative_number, startup_error


DIGITAL_INPUTS = (
    ("forward", "LI1 · Marcha adelante", False),
    ("reverse", "LI2 · Marcha atrás", False),
    ("ps2", "LI3 · PS2", False),
    ("ps4", "LI4 · PS4", False),
)
REFERENCES = (
    ("reference", "Palabra directa · 0,1 Hz", True),
    ("ai1", "AI1 · 0–10 V (mV)", True),
    ("ai2", "AI2 · ±10 V (mV con signo)", True),
    ("ai3", "AI3 · Corriente (µA)", True),
)
REFERENCE_MODES = ("Automática", "Potenciómetro", "%MW", "AI1", "AI2", "AI3")
OUTPUTS = (
    ("ready", "Variador listo", False),
    ("moving", "Motor en movimiento", False),
    ("backward", "Giro inverso", False),
    ("frequency", "rFr · Salida en 0,1 Hz", True),
)
POT_OUTPUT = ("pot_word", "Potenciómetro · Salida 0,1 Hz", True)
PARAMETERS = (
    ("acc", "ACC · Aceleración (s)", "3,0", 6000),
    ("dec", "dEC · Deceleración (s)", "3,0", 6000),
    ("lsp", "LSP · Frecuencia baja (Hz)", "0", 599),
    ("hsp", "HSP · Frecuencia alta (Hz)", "50", 599),
    ("frs", "FrS · Frecuencia nominal (Hz)", "50", 599),
    ("tfr", "tFr · Frecuencia máxima (Hz)", "60", 599),
    ("local_hz", "Potenciómetro inicial (Hz)", "30", 599),
    ("sp2", "SP2 · Velocidad 1 (Hz)", "10", 599),
    ("sp3", "SP3 · Velocidad 2 (Hz)", "15", 599),
    ("sp4", "SP4 · Velocidad 3 (Hz)", "20", 599),
)
MODES = ("trn · Transición", "LEL · Nivel", "PFO · Prioridad adelante")


@dataclass(frozen=True)
class Parameters:
    acc: float
    dec: float
    lsp: float
    hsp: float
    frs: float
    tfr: float
    local_hz: float
    mode: str
    sp2: float = 10
    sp3: float = 15
    sp4: float = 20


def preset_reference(ps2, ps4, base, settings):
    index = int(ps2) + 2 * int(ps4)
    return (base, settings.sp2, settings.sp3, settings.sp4)[index], (
        "", "SP2", "SP3", "SP4"
    )[index]


def analog_reference(channel, word, settings, current_min=4):
    signed = word - 65536 if channel == "ai2" and word >= 32768 else word
    signal = signed / 1000
    minimum, maximum = (current_min, 20) if channel == "ai3" else (0, 10)
    bipolar = channel == "ai2"
    fraction = (abs(signal) if bipolar else signal) - minimum
    fraction = min(1.0, max(0.0, fraction / (maximum - minimum)))
    hz = settings.lsp + fraction * (settings.hsp - settings.lsp)
    if bipolar and signal < 0:
        hz = -hz
    return hz, f"{channel.upper()} {signal:+.2f} {'mA' if channel == 'ai3' else 'V'}"


class Drive:
    def __init__(self, settings):
        self.settings = settings
        self.inputs = None
        self.command = 0
        self.target = 0.0
        self.frequency = 0.0
        self.position = 0.0

    def update(self, inputs, reference, dt):
        forward, reverse = inputs
        direction = int(forward) - int(reverse)
        mode = self.settings.mode
        if mode == MODES[2] and forward:
            direction = 1
        if mode != MODES[0] or direction == 0:
            self.command = direction
        elif self.inputs is None:
            self.command = 0
        elif not self.inputs[0 if direction > 0 else 1]:
            self.command = direction
        elif self.command != direction:
            self.command = 0
        self.inputs = inputs
        magnitude = min(self.settings.hsp, max(self.settings.lsp, abs(reference)))
        self.target = self.command * (-magnitude if reference < 0 else magnitude)
        if self.frequency * self.target < 0:
            dt = self.ramp(0.0, self.settings.dec, dt)
            if self.frequency != 0:
                return
        ramp = self.settings.acc if abs(self.target) > abs(self.frequency) else self.settings.dec
        self.ramp(self.target, ramp, dt)

    def ramp(self, target, duration, dt):
        start = self.frequency
        travel_time = abs(target - start) * duration / self.settings.frs
        elapsed = min(dt, travel_time)
        self.frequency = start + (target - start) * elapsed / travel_time if elapsed < travel_time else target
        self.position += (start + self.frequency) * elapsed / 2
        if elapsed < dt and self.frequency == target:
            # Only the final target is held for the remainder of the sample.
            self.position += target * (dt - elapsed)
        return dt - elapsed

    def disconnect(self):
        self.inputs = None
        self.command = 0
        self.target = self.frequency = 0.0


class ATV320Simulator:
    def __init__(self, parent, endpoint, exchange, save, compatibility):
        self.parent = parent
        self.endpoint = endpoint
        self.exchange = exchange
        self.save = save
        self.compatibility = compatibility
        self.fields = {}
        self.controls = []
        self.cancel = None
        self.bus_result = queue.Queue()
        self.pending = False
        self.timer = None
        self.connection_status = tk.StringVar(value="En pausa")
        self.demo_command = tk.IntVar()
        self.demo_ps2 = tk.BooleanVar()
        self.demo_ps4 = tk.BooleanVar()
        self.pot_value = tk.DoubleVar()
        self.pot_text = tk.StringVar(value="30,0 Hz")
        self.readings = (False, False)
        self.presets = (False, False)
        self.reference = 0.0
        self.reference_label = ""
        self.drive = None

        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x")
        self.start_button = ttk.Button(toolbar, text="Iniciar", command=self.toggle)
        self.start_button.pack(side="left")
        ttk.Label(toolbar, textvariable=self.connection_status, wraplength=770).pack(side="left", padx=10)

        self.canvas = tk.Canvas(parent, height=310, background="#eaf0f4", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=6)
        self.canvas.bind("<Configure>", lambda event: self.draw())

        settings = ttk.Frame(parent)
        settings.pack(fill="x")
        for column in range(3):
            settings.columnconfigure(column, weight=1, uniform="atv")
        command = self.panel(settings, 0, "Mando y referencia")
        reference = self.panel(settings, 1, "Referencia Fr1 y salidas PLC")
        parameters = self.panel(settings, 2, "Parámetros del variador")
        self.field(command, 0, "source", "Origen", "Prueba local", ("Prueba local", "PLC"))
        for row, (key, label, _) in enumerate(DIGITAL_INPUTS, 1):
            self.field(command, row, key, label)
        ttk.Label(command, text="Bits %M, %MX o %MWn.bit").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=3,
        )
        demo = ttk.Frame(command)
        demo.grid(row=6, column=0, columnspan=2, sticky="w")
        self.demo_buttons = []
        for value, label in ((-1, "Atrás"), (0, "Paro"), (1, "Adelante")):
            button = ttk.Radiobutton(demo, text=label, variable=self.demo_command, value=value, state="disabled")
            button.pack(side="left", padx=(0, 8))
            self.demo_buttons.append(button)
        switches = ttk.Frame(command)
        switches.grid(row=7, column=0, columnspan=2, sticky="w")
        for label, var in (("LI3 / PS2", self.demo_ps2), ("LI4 / PS4", self.demo_ps4)):
            button = ttk.Checkbutton(switches, text=label, variable=var, state="disabled")
            button.pack(side="left", padx=(0, 8))
            self.demo_buttons.append(button)
        ttk.Label(command, text="Potenciómetro (Hz)").grid(row=8, column=0, sticky="w", pady=(6, 0))
        ttk.Label(command, textvariable=self.pot_text).grid(row=8, column=1, sticky="e", pady=(6, 0))
        self.pot = tk.Scale(
            command, from_=0, to=50, resolution=0.1, orient="horizontal",
            showvalue=False, variable=self.pot_value, command=self.turn_pot, highlightthickness=0,
        )
        self.pot.grid(row=9, column=0, columnspan=2, sticky="ew")
        self.pot.bind("<ButtonRelease-1>", lambda event: self.save())
        self.pot.bind("<KeyRelease>", lambda event: self.save())
        self.field(command, 10, *POT_OUTPUT[:2])
        self.field(reference, 0, "reference_mode", "Fr1 · Fuente", REFERENCE_MODES[0], REFERENCE_MODES)
        for row, (key, label, _) in enumerate(REFERENCES, 1):
            self.field(reference, row, key, label)
        self.field(reference, 5, "ai3_range", "CrL3 · Escala AI3", "4–20 mA", ("4–20 mA", "0–20 mA"))
        for row, (key, label, _) in enumerate(OUTPUTS, 6):
            self.field(reference, row, key, label)
        for row, (key, label, default, _) in enumerate(PARAMETERS):
            self.field(parameters, row, key, label, default)
        self.field(parameters, len(PARAMETERS), "mode", "tCt · Mando 2 hilos", MODES[0], MODES)
        self.sync_pot()

    def turn_pot(self, value):
        number = f"{float(value):.1f}".replace(".", ",")
        self.fields["local_hz"].set(number)
        self.pot_text.set(f"{number} Hz")

    def sync_pot(self):
        try:
            low, high, value = (nonnegative_number(self.fields[key].get(), key)
                                for key in ("lsp", "hsp", "local_hz"))
        except ValueError:
            return
        if 0 <= low <= high <= 599:
            self.pot.configure(from_=low, to=high)
            actual = max(low, min(high, value))
            self.pot_value.set(actual)
            self.pot_text.set(f"{actual:.1f} Hz".replace(".", ","))

    @staticmethod
    def panel(parent, column, title):
        frame = ttk.LabelFrame(parent, text=title, padding=7)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 6))
        frame.columnconfigure(0, weight=1)
        return frame

    def field(self, parent, row, key, label, default="", choices=()):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=1)
        var = self.fields[key] = tk.StringVar(value=default)
        control = (ttk.Combobox(parent, textvariable=var, values=choices, width=16, state="readonly")
                   if choices else ttk.Entry(parent, textvariable=var, width=17))
        control.grid(row=row, column=1, sticky="ew", pady=1)
        self.controls.append(control)
        control.bind("<<ComboboxSelected>>", lambda event: self.save())
        control.bind("<FocusOut>", lambda event: self.save())
        if key in ("lsp", "hsp", "local_hz"):
            control.bind("<FocusOut>", lambda event: self.sync_pot(), add="+")

    def configuration(self):
        values = {}
        for key, label, _, maximum in PARAMETERS:
            raw = self.fields[key].get().strip()
            value = nonnegative_number(raw, f"ATV320 / {label}")
            if value > maximum or (key == "frs" and value == 0):
                raise ValueError(f"ATV320 / {configured(label, raw)}. Máximo {maximum}. FrS debe ser mayor que cero.")
            values[key] = value
        if not values["lsp"] <= values["hsp"] <= values["tfr"]:
            detail = " · ".join(configured(key, self.fields[key].get()) for key in ("lsp", "hsp", "tfr"))
            raise ValueError(f"ATV320 / {detail}. Debe cumplirse LSP ≤ HSP ≤ tFr.")
        mode = self.fields["mode"].get()
        source = self.fields["source"].get()
        if mode not in MODES or source not in ("Prueba local", "PLC"):
            raise ValueError(f"ATV320 / {configured('Origen', source)} · {configured('tCt', mode)}.")
        return Parameters(**values, mode=mode), source

    def map_addresses(self, reference_key):
        fields = {}
        selected = (item for item in REFERENCES if item[0] == reference_key)
        outputs = (*OUTPUTS, POT_OUTPUT) if reference_key is None else OUTPUTS
        for key, label, word in (*DIGITAL_INPUTS, *selected, *outputs):
            raw = self.fields[key].get().strip()
            title = f"ATV320 / {label}"
            try:
                address = parse_address(raw) if raw else None
                if address is not None and word == (address.coil or address.bit is not None):
                    raise ValueError("Introduce una palabra %MW completa." if word else "Introduce un bit de memoria.")
                if key == "forward" and address is None:
                    raise ValueError("Asigna el bit de marcha adelante para usar el PLC.")
                if key == reference_key and address is None:
                    raise ValueError("Asigna la palabra %MW para la fuente Fr1 seleccionada.")
            except ValueError as error:
                raise ValueError(f"{configured(title, raw)}. {error}") from None
            fields[key] = AddressField(title, raw, address)
        check_addresses(fields.values())
        bits = [field for field in fields.values() if field.address and field.address.bit is not None]
        for field in fields.values():
            if field.address is not None and not field.address.coil and field.address.bit is None:
                check_addresses((field,), bits, shared_word=True)
        return {key: field.address for key, field in fields.items()}

    def load(self, settings):
        for key, var in self.fields.items():
            if key in settings:
                var.set(str(settings[key]))
        self.sync_pot()

    def reset_session(self):
        self.pause()
        self.drive = None
        self.reference = 0.0
        self.reference_label = ""
        self.draw()

    def toggle(self):
        if self.cancel is not None:
            self.pause()
            return
        try:
            parameters, source = self.configuration()
            target = self.endpoint() if source == "PLC" else None
            mode = self.fields["reference_mode"].get()
            if mode not in REFERENCE_MODES or self.fields["ai3_range"].get() not in ("0–20 mA", "4–20 mA"):
                raise ValueError("ATV320 / Selecciona una fuente Fr1 y una escala AI3 válidas.")
            reference_key = {
                "%MW": "reference", "AI1": "ai1", "AI2": "ai2", "AI3": "ai3",
            }.get(mode)
            if mode == "Automática" and self.fields["reference"].get().strip():
                reference_key = "reference"
            addresses = self.map_addresses(reference_key) if target else {}
        except ValueError as error:
            startup_error(self.parent, "ATV320", self.connection_status, error)
            return
        if self.pending:
            self.connection_status.set("Esperando el cierre de la consulta Modbus anterior.")
            return
        self.save()
        self.drive = Drive(parameters)
        self.cancel = threading.Event()
        self.demo_command.set(0)
        self.demo_ps2.set(False)
        self.demo_ps4.set(False)
        self.sync_pot()
        self.readings = (False, False)
        self.presets = (False, False)
        self.reference = parameters.local_hz
        self.reference_label = "Potenciómetro"
        self.reference_sample = None
        self.reference_key = reference_key if target else None
        self.current_min = 4 if self.fields["ai3_range"].get() == "4–20 mA" else 0
        self.target_endpoint = target
        self.addresses = addresses
        self.allow_mask_fallback = self.compatibility.get()
        self.connected = False
        self.last_tick = time.monotonic()
        self.next_poll = self.last_tick
        self.start_button.configure(text="Pausar")
        for control in self.controls:
            control.configure(state="disabled")
        for button in self.demo_buttons:
            button.configure(state="disabled" if target else "normal")
        self.pot.configure(state="disabled" if self.reference_key else "normal")
        self.connection_status.set("Conectando con el PLC…" if target else "Prueba local · sin tráfico Modbus")
        self.tick()

    def pause(self):
        if self.cancel is None:
            return
        self.cancel.set()
        self.cancel = None
        if self.timer is not None:
            self.parent.after_cancel(self.timer)
            self.timer = None
        self.drive.disconnect()
        self.readings = (False, False)
        self.presets = (False, False)
        self.start_button.configure(text="Iniciar")
        self.connection_status.set("En pausa")
        for control in self.controls:
            control.configure(state="readonly" if isinstance(control, ttk.Combobox) else "normal")
        for button in self.demo_buttons:
            button.configure(state="disabled")
        self.pot.configure(state="normal")
        self.draw()

    def tick(self):
        now = time.monotonic()
        dt = min(0.2, now - self.last_tick)
        self.last_tick = now
        if not self.target_endpoint:
            command = self.demo_command.get()
            self.readings = (command == 1, command == -1)
            self.presets = (self.demo_ps2.get(), self.demo_ps4.get())
        if not self.target_endpoint or self.connected:
            base, label = self.reference_sample or (self.pot_value.get(), "Potenciómetro")
            self.reference, preset = preset_reference(*self.presets, base, self.drive.settings)
            self.reference_label = preset or label
            self.drive.update(self.readings, self.reference, dt)
        if self.target_endpoint and not self.pending and now >= self.next_poll:
            self.pending = True
            frequency = self.drive.frequency
            feedback = {
                "ready": int(self.connected), "moving": int(abs(frequency) >= 0.05),
                "backward": int(frequency <= -0.05), "frequency": round(frequency * 10) & 0xFFFF,
                "pot_word": round(self.pot_value.get() * 10),
            }
            cancel = self.cancel
            threading.Thread(target=self.communicate, daemon=True, args=(
                cancel, self.target_endpoint, self.addresses, self.allow_mask_fallback, feedback,
            )).start()
            self.parent.after(20, self.poll, cancel)
        self.draw()
        self.timer = self.parent.after(40, self.tick)

    def communicate(self, cancel, endpoint, addresses, compatibility, feedback):
        try:
            values = {key: self.exchange(*endpoint, addresses[key], cancelled=cancel)
                      if addresses[key] is not None else None
                      for key, _, _ in (*DIGITAL_INPUTS, *REFERENCES) if key in addresses}
            for key, value in feedback.items():
                address = addresses.get(key)
                if address is not None and not cancel.is_set():
                    self.exchange(*endpoint, address, value, compatibility, cancelled=cancel)
        except (OSError, ProtocolError) as error:
            self.bus_result.put((None, str(error)))
        else:
            self.bus_result.put((values, None))

    def poll(self, cancel):
        try:
            values, error = self.bus_result.get_nowait()
        except queue.Empty:
            self.parent.after(20, self.poll, cancel)
            return
        self.pending = False
        if self.cancel is not cancel:
            return
        self.next_poll = time.monotonic() + 0.1
        self.connected = error is None
        if error is not None:
            self.readings = (False, False)
            self.presets = (False, False)
            self.reference = 0.0
            self.reference_label = ""
            self.drive.disconnect()
            self.connection_status.set(f"Error Modbus · {error}")
            return
        self.readings = (bool(values["forward"]), bool(values["reverse"]))
        self.presets = (bool(values["ps2"]), bool(values["ps4"]))
        word = values.get(self.reference_key)
        if word is not None:
            self.reference_sample = ((word / 10, "%MW") if self.reference_key == "reference" else
                                     analog_reference(self.reference_key, word, self.drive.settings, self.current_min))
        source = "Memoria local" if self.target_endpoint[0] is None else "PLC conectado"
        self.connection_status.set(f"{source} · entradas y salidas actualizándose")

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        scale = min(width / 900, height / 320)
        if scale < 0.1:
            return
        left = (width - 900 * scale) / 2
        top = (height - 320 * scale) / 2

        def shape(kind, points, **style):
            coords = [(left if index % 2 == 0 else top) + value * scale
                      for index, value in enumerate(points)]
            getattr(canvas, f"create_{kind}")(*coords, **style)

        def label(x, y, text, size=12, color="#18364a"):
            shape("text", (x, y), text=text, fill=color, font=("Segoe UI", max(8, round(size * scale)), "bold"))

        drive = self.drive
        hz = drive.frequency if drive else 0
        position = drive.position if drive else 0
        active = abs(hz) >= 0.05
        speed = 100 * abs(hz) / drive.settings.hsp if drive and drive.settings.hsp else 0
        direction = "Atrás  ←" if hz < 0 and active else "Adelante  →" if active else "Parado"
        code = "OFF" if not self.cancel else "nSt" if self.target_endpoint and not self.connected else (
            "rUn" if active else "rdY"
        )
        shape("rectangle", (316, 8, 584, 158), fill="#243743", outline="#12242f", width=2)
        for x in range(327, 371, 7):
            shape("line", (x, 18, x, 146), fill="#496270", width=2)
        label(472, 28, "ATV320  ·  Variador simulado", 13, "#ffffff")
        shape("rectangle", (385, 45, 569, 96), fill="#10232b", outline="#617781")
        label(476, 70, f"{code}    {abs(hz):05.1f} Hz", 17, "#8bf1ac")
        lamps = (("LI1", self.readings[0]), ("LI2", self.readings[1]), ("LI3", self.presets[0]),
                 ("LI4", self.presets[1]), ("RUN", active))
        for index, (name, state) in enumerate(lamps):
            x = 401 + index * 37
            shape("oval", (x - 5, 112, x + 5, 122), outline="", fill="#64e591" if state else "#546d7a")
            label(x, 137, name, 10, "#ffffff")
        label(192, 67, f"Consigna   {self.reference:.1f} Hz")
        label(192, 96, f"Objetivo   {drive.target if drive else 0:+.1f} Hz")
        label(192, 124, self.reference_label, 11)
        label(707, 67, direction, 16)
        label(707, 96, f"Salida   {hz:+.1f} Hz")
        shape("line", (450, 158, 450, 181, 805, 181, 805, 241), fill="#567a92", width=3)
        shape("rectangle", (115, 245, 773, 271), fill="#253f51", outline="")
        for x in range(126, 773, 24):
            shift = (x - 126 + position * 3) % 648 + 126
            shape("line", (shift, 250, shift, 265), fill="#5f7a8a", width=2)
        for x in (115, 773):
            shape("oval", (x-13, 245, x+13, 271), fill="#9eb1bd", outline="#253f51", width=2)
        for i in range(7):
            x = 134 + (i * 90 + position * 3) % 630
            shape("polygon", (x-10, 245, x-10, 224, x-4, 217, x-4, 206,
                              x+4, 206, x+4, 217, x+10, 224, x+10, 245),
                  fill="#7ac5df", outline="#287088", width=2)
            shape("rectangle", (x-5, 202, x+5, 206), fill="#246681", outline="")
        shape("line", (786, 258, 801, 258), fill="#38576b", width=4)
        shape("oval", (801, 240, 837, 276), fill="#c2d5df", outline="#38576b", width=2)
        label(819, 258, "M")
        label(450, 295, f"Cinta de botellas   ·   {direction}   ·   {abs(hz):.1f} Hz   ·   {speed:.0f} % de HSP")
