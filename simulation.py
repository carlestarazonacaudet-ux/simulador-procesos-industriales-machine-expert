"""Simulación de un depósito con válvulas y sensores conectados por Modbus."""

import math
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from itertools import combinations, product

from modbus import Address, ProtocolError, parse_address

SAMPLE_MS = 250


def configured(label, value):
    return f"{label} = {value!r}" if value != "" else f"{label} = (vacío)"


@dataclass(frozen=True)
class AddressField:
    label: str
    raw: str
    address: Address | None

    def describe(self):
        address = self.address
        mapped = f"bobina %M{address.index}" if address.coil else f"palabra %MW{address.index}"
        if address.bit is not None:
            mapped += f", bit {address.bit}"
        return f"{configured(self.label, self.raw)} ({mapped})"


def check_addresses(first, second=None, *, shared_word=False):
    pairs = combinations(first, 2) if second is None else product(first, second)
    for left, right in pairs:
        a, b = left.address, right.address
        if a is None or b is None:
            continue
        if a == b:
            reason = "Dirección duplicada"
        elif shared_word and not a.coil and not b.coil and a.index == b.index:
            reason = f"Solapamiento en %MW{a.index}"
        else:
            continue
        raise ValueError(f"{reason}\n{left.describe()}\n{right.describe()}")


def startup_error(parent, title, status, error):
    status.set(str(error).replace("\n", " · "))
    messagebox.showerror(f"No se pudo iniciar {title}", str(error), parent=parent)


def draw_vessel(canvas, left, top, right, bottom, level, color="#38a1d1"):
    canvas.create_rectangle(left, top, right, bottom, fill="#ffffff", outline="")
    if level:
        waterline = bottom - level * (bottom - top)
        canvas.create_rectangle(left, waterline, right, bottom, fill=color, outline="")
    canvas.create_rectangle(left, top, right, bottom, outline="#29485c", width=2)


@dataclass(frozen=True)
class Valve:
    address: Address | None
    minimum: int
    maximum: int
    max_flow: float

    def decoded_value(self, word):
        if self.minimum < 0 and word >= 32768:
            return word - 65536
        return word

    def opening(self, word):
        value = self.decoded_value(word)
        return max(0.0, min(1.0, (value - self.minimum) / (self.maximum - self.minimum)))


def nonnegative_number(raw, title):
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        raise ValueError(f"{configured(title, raw)}. Introduce un número.") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{configured(title, raw)}. Introduce un número positivo o cero.")
    return value


def parse_valve(raw, low, high, max_flow, name):
    original = raw
    if raw.isascii() and raw.isdecimal():
        raw = f"%MW{raw}"
    try:
        address = parse_address(raw) if raw else None
    except ValueError as error:
        raise ValueError(f"{configured(f'{name} / Dirección', original)}. {error}") from None
    if address is not None and (address.coil or address.bit is not None):
        raise ValueError(f"{configured(f'{name} / Dirección', original)}. Se requiere una palabra %MW completa.")
    detail = f"{name} / {configured('Mínimo', low)} · {configured('Máximo', high)} · {configured('Dirección', original)}"
    try:
        minimum = int(low)
        maximum = int(high)
    except ValueError:
        raise ValueError(f"{detail}. El rango debe contener dos enteros.") from None
    upper_limit = 32767 if minimum < 0 else 65535
    if not -32768 <= minimum < maximum <= upper_limit:
        raise ValueError(f"{detail}. El rango debe ser ascendente y caber en una palabra.")
    return Valve(address, minimum, maximum, nonnegative_number(max_flow, f"El caudal de {name}"))


@dataclass(frozen=True)
class Sensor:
    threshold: float
    address: Address | None


@dataclass(frozen=True)
class Tank:
    capacity: float
    inlet: Valve
    outlet: Valve
    sensors: tuple[Sensor, Sensor]

    def advance(self, volume, openings, seconds):
        incoming = openings[0] * self.inlet.max_flow
        outgoing = openings[1] * self.outlet.max_flow
        return max(0.0, min(self.capacity, volume + (incoming - outgoing) * seconds / 60))


class TankSimulator:
    defaults = {
        "capacidad": "100",
        "entrada_direccion": "", "entrada_min": "0", "entrada_max": "100", "entrada_caudal": "20",
        "salida_direccion": "", "salida_min": "0", "salida_max": "100", "salida_caudal": "20",
        "s1_direccion": "", "s1_nivel": "25", "s2_direccion": "", "s2_nivel": "75",
    }

    def __init__(self, parent, endpoint, exchange, save, compatibility):
        self.parent = parent
        self.endpoint = endpoint
        self.exchange = exchange
        self.save = save
        self.compatibility = compatibility
        self.saved = {}
        self.fields = {}
        self.editors = []
        self.config = None
        self.cancel = None
        self.in_flight = False
        self.timer = None
        self.results = queue.Queue()
        self.volume = 0.0
        self.openings = None
        self.words = (None, None)
        self.sample_time = None
        self.confirmed = (None, None)
        self.status = tk.StringVar(value="Introduce al menos una dirección %MW, por ejemplo %MW100 o 100.")

        selector = ttk.Frame(parent)
        selector.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(selector, text="Iniciar", command=self.toggle)
        self.start_button.pack(side="left")

        content = ttk.Frame(parent)
        content.pack(fill="both", expand=True)
        controls = ttk.Frame(content)
        controls.pack(side="left", anchor="n", padx=(0, 14))
        self.field(controls, "capacidad", "Capacidad (L)", 0, 0)

        valves = ttk.LabelFrame(controls, text="Válvulas", padding=8)
        valves.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        for column, (key, title) in enumerate((("entrada", "Entrada"), ("salida", "Salida")), start=1):
            ttk.Label(valves, text=title).grid(row=0, column=column, sticky="w", padx=4)
            for row, (suffix, description) in enumerate((
                ("direccion", "Dirección %MW"), ("min", "Valor mínimo"),
                ("max", "Valor máximo"), ("caudal", "Caudal máximo (L/min)"),
            ), start=1):
                self.field(valves, f"{key}_{suffix}", description, row, column, shared_label=True)
        ttk.Label(valves, text="Una dirección vacía deja esa válvula cerrada.").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(3, 0),
        )

        sensors = ttk.LabelFrame(controls, text="Sensores de nivel", padding=8)
        sensors.grid(row=2, column=0, sticky="ew")
        for column, key in enumerate(("s1", "s2"), start=1):
            ttk.Label(sensors, text=f"Sensor {column}").grid(row=0, column=column, sticky="w", padx=4)
            self.field(sensors, f"{key}_nivel", "Umbral (L)", 1, column, shared_label=True)
            self.field(sensors, f"{key}_direccion", "Dirección BOOL (opcional)", 2, column, shared_label=True)

        display = ttk.Frame(content)
        display.pack(side="left", fill="both", expand=True)
        self.summary = tk.StringVar()
        self.flows = tk.StringVar()
        self.calibration = tk.StringVar()
        ttk.Label(display, textvariable=self.summary, font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(display, textvariable=self.flows).pack(anchor="w", pady=(2, 4))
        ttk.Label(display, textvariable=self.calibration).pack(anchor="w", pady=(0, 4))
        self.canvas = tk.Canvas(display, background="#f1f5f9", highlightthickness=0, height=230)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.draw())
        ttk.Label(parent, textvariable=self.status, wraplength=980).pack(anchor="w", pady=(6, 0))
        for variable in self.fields.values():
            variable.trace_add("write", self.preview)
        self.draw()

    def field(self, parent, key, title, row, column, shared_label=False):
        if not shared_label or column == 1:
            ttk.Label(parent, text=title).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        value = tk.StringVar(value=self.defaults[key])
        editor = ttk.Entry(parent, textvariable=value, width=12)
        editor.grid(row=row, column=column if shared_label else 1, sticky="w", padx=4, pady=2)
        self.fields[key] = value
        self.editors.append(editor)

    def load(self, saved):
        if not isinstance(saved, dict):
            return
        self.saved = {key: saved[key] for key in self.fields if isinstance(saved.get(key), str)}
        for key, value in self.saved.items():
            self.fields[key].set(value)
        self.draw()

    def reset_session(self):
        self.config = None
        self.volume = 0.0
        self.openings = None
        self.words = (None, None)
        self.status.set("Introduce al menos una dirección %MW, por ejemplo %MW100 o 100.")
        self.draw()

    def configuration(self, fields):
        capacity = nonnegative_number(fields["capacidad"], "Depósito / Capacidad (L)")
        if capacity == 0:
            raise ValueError(f"{configured('Depósito / Capacidad (L)', fields['capacidad'])}. Debe ser mayor que cero.")
        valves = [
            parse_valve(fields[f"{key}_direccion"], fields[f"{key}_min"], fields[f"{key}_max"],
                        fields[f"{key}_caudal"], f"Válvulas / {key.capitalize()}")
            for key in ("entrada", "salida")
        ]
        if all(valve.address is None for valve in valves):
            raise ValueError("Válvulas / Entrada y salida: ambas direcciones están vacías. Introduce al menos una palabra %MW.")
        valve_fields = [
            AddressField(f"Válvulas / {key.capitalize()} / Dirección", fields[f"{key}_direccion"], valve.address)
            for key, valve in zip(("entrada", "salida"), valves)
        ]
        sensors = []
        sensor_fields = []
        for index in (1, 2):
            raw = fields[f"s{index}_direccion"]
            level = fields[f"s{index}_nivel"]
            name = f"Sensores de nivel / Sensor {index}"
            threshold = nonnegative_number(level, f"{name} / Umbral (L)")
            if threshold > capacity:
                raise ValueError(f"{name}: {configured('Umbral (L)', level)} supera la capacidad de {capacity:g} L. "
                                 f"{configured('Bit', raw)}.")
            try:
                address = parse_address(raw) if raw else None
            except ValueError as error:
                raise ValueError(f"{configured(f'{name} / Bit', raw)}. {error}") from None
            if address is not None and not address.coil:
                if address.bit is None:
                    raise ValueError(f"{configured(f'{name} / Bit', raw)}. Se requiere una dirección booleana.")
            sensors.append(Sensor(threshold, address))
            sensor_fields.append(AddressField(f"{name} / Bit", raw, address))
        check_addresses(sensor_fields)
        check_addresses(sensor_fields, valve_fields, shared_word=True)
        return Tank(capacity, *valves, tuple(sensors))

    def toggle(self):
        if self.cancel is not None:
            self.pause()
            return
        fields = {key: field.get().strip() for key, field in self.fields.items()}
        try:
            config = self.configuration(fields)
            endpoint = self.endpoint()
        except ValueError as error:
            startup_error(self.parent, "el depósito", self.status, error)
            return
        old_settings, self.saved = self.saved, fields
        if not self.save():
            self.saved = old_settings
            self.status.set("No se pudo guardar la configuración del depósito.")
            return
        self.config = config
        self.volume = min(self.volume, config.capacity)
        self.openings = None
        self.words = (None, None)
        self.sample_time = None
        self.confirmed = (None, None)
        self.cancel = threading.Event()
        self.target = endpoint
        self.start_button.configure(text="Pausar")
        for editor in self.editors:
            editor.configure(state="disabled")
        self.status.set(f"Leyendo consignas en {endpoint[0]}:{endpoint[1]} (unidad {endpoint[2]})…")
        self.draw()
        self.schedule(0)

    def pause(self):
        if self.cancel is None:
            return
        self.cancel.set()
        self.cancel = None
        if self.timer is not None:
            self.parent.after_cancel(self.timer)
            self.timer = None
        self.start_button.configure(text="Iniciar")
        for editor in self.editors:
            editor.configure(state="normal")
        self.status.set("Simulación en pausa.")

    def schedule(self, delay=SAMPLE_MS):
        if self.cancel is not None and not self.in_flight and self.timer is None:
            self.timer = self.parent.after(delay, self.sample)

    def sample(self):
        self.timer = None
        self.in_flight = True
        cancellation = self.cancel
        args = (cancellation, self.config, self.target, self.compatibility.get(), self.volume,
                self.openings, self.sample_time, self.confirmed)
        threading.Thread(target=self.cycle, args=args, daemon=True).start()
        self.parent.after(15, self.poll, cancellation)

    def cycle(self, cancellation, config, endpoint, compatibility, volume, openings, old_time, confirmed):
        errors = (OSError, ProtocolError)
        readings = [0.0, 0.0]
        words = [None, None]
        failures = []
        for index, (name, valve) in enumerate((("Entrada", config.inlet), ("Salida", config.outlet))):
            if valve.address is None:
                continue
            try:
                word = self.exchange(*endpoint, valve.address, cancelled=cancellation)
                if cancellation.is_set():
                    self.results.put(None)
                    return
                words[index] = valve.decoded_value(word)
                readings[index] = valve.opening(word)
            except errors as error:
                readings[index] = None
                failures.append(f"{name} %MW{valve.address.index}: {error}")
        if failures:
            self.results.put((volume, tuple(readings), tuple(words), None, (None, None),
                              "Nivel congelado. " + " ".join(failures)))
            return

        now = time.monotonic()
        if old_time is not None:
            volume = config.advance(volume, openings, now - old_time)
        states = list(confirmed)
        for index, sensor in enumerate(config.sensors):
            if cancellation.is_set():
                self.results.put(None)
                return
            state = int(volume >= sensor.threshold)
            if sensor.address is not None and state != states[index]:
                try:
                    self.exchange(*endpoint, sensor.address, state, compatibility, cancelled=cancellation)
                    states[index] = state
                except errors as error:
                    states[index] = None
                    failures.append(f"Sensor {index + 1}: {error}")
        self.results.put((volume, tuple(readings), tuple(words), now, tuple(states), " ".join(failures)))

    def poll(self, cancellation):
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.parent.after(15, self.poll, cancellation)
            return
        self.in_flight = False
        if self.cancel is cancellation and result is not None:
            self.volume, self.openings, self.words, self.sample_time, self.confirmed, error = result
            self.status.set(error or "Simulación en marcha.")
            self.draw()
        self.schedule(0 if self.cancel is not cancellation else SAMPLE_MS)

    def preview(self, *_):
        if self.cancel is None:
            self.openings = None
            self.words = (None, None)
            self.draw()

    def draw(self):
        try:
            capacity = nonnegative_number(self.fields["capacidad"].get(), "La capacidad") or 1
        except ValueError:
            capacity = self.config.capacity if self.config else 100
        volume = min(capacity, self.volume)
        level = volume / capacity
        self.summary.set(f"Llenado {level:.1%}     {volume:.2f} / {capacity:g} L")
        self.draw_openings()

        canvas = self.canvas
        canvas.delete("all")
        width, height = max(270, canvas.winfo_width()), max(160, canvas.winfo_height())
        left, right, top, bottom = width * 0.25, width * 0.75, 22, height - 23
        draw_vessel(canvas, left, top, right, bottom, level)
        for index in (1, 2):
            try:
                threshold = nonnegative_number(self.fields[f"s{index}_nivel"].get(), "El umbral")
            except ValueError:
                continue
            if threshold > capacity:
                continue
            y = bottom - (threshold / capacity) * (bottom - top)
            active = volume >= threshold
            color = "#167642" if active else "#64748b"
            canvas.create_line(left, y, right, y, dash=(4, 3), fill=color, width=2)
            side = -1 if index == 1 else 1
            edge = left if side < 0 else right
            marker = edge + side * 10
            canvas.create_line(edge, y, marker, y, fill=color)
            canvas.create_oval(marker - 5, y - 5, marker + 5, y + 5,
                               fill=color if active else "#f1f5f9", outline=color, width=2)
            status = "Activo" if active else "Inactivo"
            canvas.create_text(marker + side * 9, y, anchor="e" if side < 0 else "w",
                               justify="right" if side < 0 else "left", fill="#243746",
                               text=f"S{index} · {threshold:g} L\n{status}")

    def draw_openings(self):
        flows = []
        calibration = []
        for index, (key, name) in enumerate((("entrada", "Entrada"), ("salida", "Salida"))):
            try:
                minimum = int(self.fields[f"{key}_min"].get())
                maximum = int(self.fields[f"{key}_max"].get())
                if minimum < maximum:
                    calibration.append(f"{name}  {minimum} → 0 %   {(minimum + maximum) / 2:g} → 50 %   {maximum} → 100 %")
            except ValueError:
                pass
            if not self.fields[f"{key}_direccion"].get().strip():
                flows.append(f"{name} cerrada · 0 % · 0 L/min")
            elif self.openings is None or self.openings[index] is None or self.words[index] is None:
                flows.append(f"{name} · valor %MW pendiente")
            else:
                valve = (self.config.inlet, self.config.outlet)[index]
                opening = self.openings[index]
                flows.append(
                    f"{name} %MW{valve.address.index} = {self.words[index]} → "
                    f"{opening:.1%} · {opening * valve.max_flow:.2f} L/min"
                )
        self.flows.set("\n".join(flows))
        self.calibration.set("\n".join(calibration))
