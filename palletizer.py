"""Simulador de paletizado con un eje cartesiano y recogida por vacío."""

import queue
import random
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import ttk

from conveyor import SIGNALS, ConveyorSimulator
from modbus import ProtocolError
from simulation import AddressField, check_addresses, configured, nonnegative_number, startup_error

MODES = ("Encoder", "Palabra de posición", "Bits de posición")
POSITIONS = ("Recogida", "Estación 1", "Estación 2", "Estación 3", "Pila de palets")
TICK_MS = 90
BELT_STOP = 1.35


@dataclass(frozen=True)
class VacuumGauge:
    start: float
    target: float
    threshold: float
    min_time: float
    max_time: float

    def pressure(self, elapsed, duration):
        fraction = 1.0 if duration <= 0 else min(1.0, elapsed / duration)
        return self.start + (self.target - self.start) * fraction


@dataclass(frozen=True)
class PalletConfig:
    mode: str
    addresses: dict
    counts_per_position: int
    tolerance: int
    speed: float
    min_speed: float
    max_speed: float
    min_word: int
    max_word: int
    interval: float
    stock: int
    gauge: VacuumGauge | None = None

    @property
    def reach(self):
        return self.tolerance / self.counts_per_position if self.mode == "Encoder" else .015

    def movement_speed(self, words):
        if self.addresses["velocidad_mw"] is None:
            return self.speed
        raw = words["velocidad_mw"]
        if raw is None:
            return 0.0
        fraction = min(1.0, max(0.0, (raw - self.min_word) / (self.max_word - self.min_word)))
        return self.min_speed + fraction * (self.max_speed - self.min_speed)


@dataclass(frozen=True)
class PalletState:
    position: float = 0.0
    target: float = 0.0
    stations: tuple[int, int, int] = (-1, -1, -1)
    stock: int = 12
    cargo: str = ""
    vacuum: bool = False
    feed: float = 0.0
    active: frozenset[str] = frozenset()
    event: str = ""
    vertical: float = 0.0
    vertical_target: float = 0.0
    reached_stop: bool = False
    broken: int = 0
    encoder_z_active: bool = False
    pressure: float = 0.0
    vacuum_elapsed: float = 0.0
    vacuum_duration: float = 0.0

    def box_centered(self, settings):
        tolerance = max(1e-9, settings.reach)
        return self.reached_stop and abs(self.feed - 1.0) <= tolerance

    def step(self, settings, values, elapsed):
        target, event = self.target, self.event
        active = frozenset(key for key, value in values.items() if value)
        rising = active - self.active
        if settings.mode == "Encoder" and "ejecutar" in rising and values["pulsos"] is not None:
            word = values["pulsos"]
            pulses = word - 65536 if word >= 32768 else word
            target = min(4.0, max(0.0, self.position + pulses / settings.counts_per_position))
        elif settings.mode == "Palabra de posición" and values["destino"] in range(5):
            target = float(values["destino"])
        elif settings.mode == "Bits de posición":
            commands = [index for index in range(5) if f"ir{index}" in active]
            if len(commands) == 1:
                target = float(commands[0])
        stations = list(self.stations)
        for index in range(3):
            if f"retirar{index}" in rising:
                stations[index] = -1
            elif f"poner{index}" in rising and stations[index] < 0:
                stations[index] = 0
        distance = target - self.position
        advance = settings.movement_speed(values) * elapsed
        position = self.position + max(-advance, min(advance, distance))
        if abs(position - target) < 1e-10:
            position = target
        nearest = round(position)
        stopped = position == target
        in_position = stopped and abs(position - nearest) <= settings.reach
        vertical_target = self.vertical_target
        encoder_z_active = self.encoder_z_active
        vertical_step = values.get("pulsos_z")
        if settings.mode == "Encoder" and "ejecutar_z" in rising and vertical_step is not None:
            pulses = vertical_step - 65536 if vertical_step >= 32768 else vertical_step
            vertical_target = max(0.0, min(1.0, self.vertical + pulses / settings.counts_per_position))
            encoder_z_active = True
        elif ("subir" in active) != ("bajar" in active):
            vertical_target = float("bajar" in active)
            encoder_z_active = False
        elif "subir" in active or "bajar" in active or not encoder_z_active or settings.mode != "Encoder":
            vertical_target = self.vertical
            encoder_z_active = False
        distance_z = vertical_target - self.vertical
        vertical = self.vertical + max(-advance, min(advance, distance_z))
        if abs(vertical - vertical_target) < 1e-10:
            vertical = vertical_target
            encoder_z_active = False
        lowered = vertical == vertical_target and 1.0 - vertical <= settings.reach
        belt_direction = int("cinta_adelante" in active) - int("cinta_atras" in active)
        feed = max(0.0, min(BELT_STOP, self.feed + belt_direction * elapsed / settings.interval))
        if self.reached_stop and self.feed > 1.0 > feed:
            feed = 1.0
        reached_stop = self.reached_stop or feed >= BELT_STOP
        centered = reached_stop and abs(feed - 1.0) <= max(1e-9, settings.reach)
        cargo, stock = self.cargo, self.stock
        broken = self.broken
        vacuum = self.vacuum if values["vacio"] is None else bool(values["vacio"])
        gauge = settings.gauge
        pressure, vacuum_elapsed, vacuum_duration = 0.0, 0.0, 0.0
        if gauge is not None:
            pressure = gauge.start
            if vacuum:
                vacuum_duration = (self.vacuum_duration if self.vacuum
                                   else random.uniform(gauge.min_time, gauge.max_time))
                vacuum_elapsed = self.vacuum_elapsed + elapsed if self.vacuum else 0.0
                pressure = gauge.pressure(vacuum_elapsed, vacuum_duration)
        suction_ready = vacuum and (gauge is None or pressure >= gauge.threshold)

        if cargo and not vacuum:
            if lowered and in_position and nearest in (1, 2, 3) and f"retirar{nearest - 1}" not in rising:
                index = nearest - 1
                if cargo == "Caja" and stations[index] >= 0:
                    stations[index] += 1
                    event = f"Caja depositada en estación {nearest}."
                elif cargo == "Palet" and stations[index] < 0:
                    stations[index] = 0
                    event = f"Palet colocado en estación {nearest}."
                else:
                    broken += 1
                    event = f"{cargo} roto al soltarlo en una estación sin apoyo compatible."
            elif lowered and in_position and nearest == 4 and cargo == "Palet":
                stock += 1
                event = "Palet devuelto a la pila."
            else:
                broken += 1
                event = f"{cargo} roto al desactivar el vacío sin apoyo."
            cargo = ""
        elif not cargo and suction_ready and in_position and lowered:
            if nearest == 0 and centered and belt_direction == 0:
                cargo, feed, reached_stop, event = "Caja", 0.0, False, "Caja recogida."
            elif nearest == 4 and stock:
                stock -= 1
                cargo, event = "Palet", "Palet recogido de la pila."
        return PalletState(position, target, tuple(stations), stock, cargo, vacuum, feed, active, event,
                           vertical, vertical_target, reached_stop, broken, encoder_z_active,
                           pressure, vacuum_elapsed, vacuum_duration)

    def outputs(self, config):
        def at(index):
            return int(abs(self.position - index) <= config.reach)

        values = {"cero": at(0), "caja": int(self.box_centered(config)),
                  "tope": int(self.feed >= BELT_STOP), "encoder": round(self.position * config.counts_per_position),
                  "encoder_z": round(self.vertical * config.counts_per_position),
                  "sensor_pila": at(4),
                  "barometro_ok": int(config.gauge is not None and self.vacuum and self.pressure >= config.gauge.threshold),
                  "sensor_z_arriba": int(self.vertical <= config.reach),
                  "sensor_z_abajo": int(1 - self.vertical <= config.reach)}
        for index, count in enumerate(self.stations):
            values.update({f"sensor{index}": at(index + 1), f"verde{index}": int(count >= 0),
                           f"rojo{index}": int(count < 0)})
        return values


class PalletizerSimulator:
    def __init__(self, parent, endpoint, exchange, save, compatibility):
        self.parent, self.endpoint, self.exchange = parent, endpoint, exchange
        self.save, self.compatibility = save, compatibility
        self.fields, self.edits, self.bit_inputs, self.outputs, self.words = {}, [], {}, {}, {}
        self.signal_inputs = {}
        self.saved = {}
        self.config = None
        self.state = PalletState()
        self.cancel = None
        self.timer = None
        self.in_flight = False
        self.results = queue.Queue()
        self.published = {}
        self.last_sample = None
        self.signals = (False,) * len(SIGNALS)
        self.displays = (None,) * 3
        self.siren_playing = False
        self.status = tk.StringVar(value="Configura el mando y pulsa Iniciar.")
        self.viewport = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.viewport.yview)
        scrollbar.pack(side="right", fill="y")
        self.viewport.configure(yscrollcommand=scrollbar.set)
        self.viewport.pack(fill="both", expand=True)
        body = ttk.Frame(self.viewport, padding=(0, 0, 8, 0))
        window = self.viewport.create_window(0, 0, window=body, anchor="nw")
        body.bind("<Configure>", lambda event: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        self.viewport.bind("<Configure>", lambda event: self.viewport.itemconfigure(window, width=event.width))
        parent.winfo_toplevel().bind("<MouseWheel>", self.scroll, add="+")

        controls = ttk.Frame(body)
        controls.pack(fill="x")
        for column in range(4):
            controls.columnconfigure(column, weight=1, uniform="controls")

        def section(title, row, column):
            panel = ttk.LabelFrame(controls, text=title, padding=6)
            panel.grid(row=row, column=column, padx=3, pady=3, sticky="nsew")
            panel.columnconfigure(1, weight=1)
            return panel

        motion = section("Eje horizontal", 0, 0)
        speed = section("Velocidad y alimentación", 0, 1)
        sensors = section("Eje vertical y vacío", 0, 2)
        pressure = section("Barómetro", 0, 3)
        alerts = section("Avisos", 1, 3)
        self.mode = tk.StringVar(value=MODES[0])
        self.fields["modo"] = self.mode
        ttk.Label(motion, text="Modo de mando").grid(row=0, column=0, sticky="w")
        choice = ttk.Combobox(motion, values=MODES, textvariable=self.mode, state="readonly", width=15)
        choice.grid(row=0, column=1, sticky="w", padx=6)
        self.edits.append((choice, "readonly"))
        self.mode_panels = {}
        for mode in MODES:
            mode_panel = ttk.Frame(motion)
            mode_panel.grid(row=1, column=0, columnspan=2, sticky="nw")
            self.mode_panels[mode] = mode_panel
        encoder = self.mode_panels[MODES[0]]
        for row, (key, label, default, namespace) in enumerate((
            ("pulsos", "Pulsos X %MW", "", self.words),
            ("ejecutar", "Ejecutar desplazamiento", "", self.bit_inputs),
            ("encoder", "Encoder X %MW", "", self.outputs),
            ("pulsos_pos", "Pulsos entre estaciones", "1000", None),
            ("tolerancia", "Tolerancia (pulsos)", "20", None),
        )):
            self.field(encoder, key, label, row, default=default, namespace=namespace)
        word = self.mode_panels[MODES[1]]
        self.field(word, "destino", "Posición %MW", 0, namespace=self.words)
        ttk.Label(word, text="0 Recogida · 1–3 Estaciones · 4 Pila", wraplength=188).grid(
            row=1, column=0, columnspan=2, sticky="w",
        )
        for index, label in enumerate(POSITIONS):
            self.field(self.mode_panels[MODES[2]], f"ir{index}", f"Ir a {label.lower()}", index,
                       namespace=self.bit_inputs)
        choice.bind("<<ComboboxSelected>>", lambda event: self.show_mode())
        self.field(motion, "cero", "Sensor inicial", 2, namespace=self.outputs)
        self.field(motion, "sensor_pila", "Sensor posición pila", 3, namespace=self.outputs)

        for row, (key, label, default, namespace) in enumerate((
            ("velocidad", "Directa (estaciones/s)", "1", None),
            ("velocidad_mw", "Mando de velocidad %MW", "", self.words),
            ("vel_min", "Velocidad mínima", "0", None),
            ("vel_max", "Velocidad máxima", "3", None),
            ("mw_min", "Valor MW al mínimo", "0", None),
            ("mw_max", "Valor MW al máximo", "100", None),
            ("intervalo", "Tiempo entre cajas (s)", "2", None),
            ("stock", "Palets iniciales en la pila", "12", None),
            ("cinta_adelante", "Cinta adelante", "", self.bit_inputs),
            ("cinta_atras", "Cinta atrás", "", self.bit_inputs),
            ("caja", "Sensor caja centrada", "", self.outputs),
            ("tope", "Sensor tope", "", self.outputs),
        )):
            self.field(speed, key, label, row, default=default, namespace=namespace)
        for row, (key, label, namespace) in enumerate((
            ("vacio", "Mando de vacío", self.bit_inputs),
            ("subir", "Subir brazo", self.bit_inputs), ("bajar", "Bajar brazo", self.bit_inputs),
            ("pulsos_z", "Pulsos Z %MW", self.words),
            ("ejecutar_z", "Ejecutar eje Z", self.bit_inputs),
            ("encoder_z", "Encoder Z %MW", self.outputs),
            ("sensor_z_arriba", "Sensor brazo arriba", self.outputs),
            ("sensor_z_abajo", "Sensor brazo abajo", self.outputs),
        )):
            self.field(sensors, key, label, row, namespace=namespace)
        self.fields["usar_barometro"] = tk.StringVar(value="0")
        gauge_toggle = ttk.Checkbutton(pressure, text="Usar barómetro", variable=self.fields["usar_barometro"],
                                       onvalue="1", offvalue="0")
        gauge_toggle.grid(row=0, column=0, columnspan=2, sticky="w")
        self.edits.append((gauge_toggle, "normal"))
        for row, (key, label, default) in enumerate((
            ("presion_inicio", "Vacío inicial (kPa)", "0"), ("presion_final", "Vacío final (kPa)", "70"),
            ("presion_umbral", "Umbral de agarre (kPa)", "60"),
            ("presion_min_s", "Tiempo mínimo (s)", "0.5"), ("presion_max_s", "Tiempo máximo (s)", "1.5"),
        ), 1):
            self.field(pressure, key, label, row, default=default)
        self.field(pressure, "barometro_ok", "Bit presión alcanzada", 6, namespace=self.outputs)
        for index in range(3):
            panel = section(POSITIONS[index + 1], 1, index)
            for row, (key, label, namespace) in enumerate((
                ("poner", "Colocar palet", self.bit_inputs), ("retirar", "Retirar palet y cajas", self.bit_inputs),
                ("sensor", "Brazo en posición", self.outputs), ("verde", "Piloto verde", self.outputs),
                ("rojo", "Piloto rojo", self.outputs), ("display", "Display %MW", self.words),
                ("nombre", "Nombre del display", None),
            )):
                self.field(panel, f"{key}{index}", label, row, namespace=namespace)
        for index, (key, name, _) in enumerate(SIGNALS):
            self.field(alerts, f"aviso_{key}", name, index, namespace=self.signal_inputs)

        actions = ttk.Frame(body)
        actions.pack(before=controls, fill="x", pady=4)
        self.start_button = ttk.Button(actions, text="Iniciar", command=self.toggle)
        self.start_button.pack(side="left")
        self.reset_button = ttk.Button(actions, text="Restablecer", command=self.reset_session)
        self.reset_button.pack(side="left", padx=6)
        ttk.Label(actions, textvariable=self.status, wraplength=820).pack(side="left", padx=6)
        self.diagram = tk.Canvas(body, background="#f1f5f9", height=250, highlightthickness=0)
        self.diagram.pack(after=actions, fill="x", pady=(0, 6))
        self.diagram.bind("<Configure>", lambda event: self.draw())
        self.show_mode()
        self.draw()

    def scroll(self, event):
        widget = event.widget
        while widget is not None:
            if widget is self.viewport:
                self.viewport.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            widget = widget.master

    def field(self, panel, key, label, row, *, default="", namespace=None):
        variable = tk.StringVar(value=default)
        self.fields[key] = variable
        if namespace is not None:
            namespace[key] = label
        ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
        editor = ttk.Entry(panel, textvariable=variable, width=10)
        editor.grid(row=row, column=1, sticky="ew", pady=1)
        self.edits.append((editor, "normal"))

    def show_mode(self):
        for mode, panel in self.mode_panels.items():
            if mode == self.mode.get():
                panel.grid()
            else:
                panel.grid_remove()

    def load(self, settings):
        self.saved = {key: value for key, value in settings.items() if key in self.fields}
        for key, value in self.saved.items():
            self.fields[key].set(value)
        self.show_mode()

    @staticmethod
    def integer(fields, key, low, high, name):
        try:
            value = int(fields[key])
        except ValueError:
            value = low - 1
        if not low <= value <= high:
            raise ValueError(f"{configured(name, fields[key])}. Usa un entero de {low} a {high}.")
        return value

    def configuration(self, fields):
        mode = fields["modo"]
        if mode not in MODES:
            raise ValueError(f"{configured('Movimiento / Modo', mode)}. Selecciona un modo de mando.")
        used = {
            MODES[0]: {"pulsos", "ejecutar", "encoder", "pulsos_z", "ejecutar_z", "encoder_z"},
            MODES[1]: {"destino"},
            MODES[2]: {f"ir{index}" for index in range(5)},
        }
        motion_keys = set().union(*used.values())
        addresses, inputs, outputs = {}, [], []
        for namespace in (self.bit_inputs, self.words, self.outputs, self.signal_inputs):
            for key, label in namespace.items():
                if key in motion_keys and key not in used[mode]:
                    addresses[key] = None
                    continue
                raw = fields[key]
                parser = (ConveyorSimulator.word if namespace is self.words or key in ("encoder", "encoder_z")
                          else ConveyorSimulator.bit)
                address = parser(raw, label)
                addresses[key] = address
                station_key = key.rstrip("012")
                title = f"Estación {int(key[-1]) + 1} / {label}" if station_key in (
                    "poner", "retirar", "sensor", "verde", "rojo",
                ) else label
                item = AddressField(title, raw, address)
                if namespace is self.outputs:
                    outputs.append(item)
                elif namespace is not self.signal_inputs and not key.startswith("display"):
                    inputs.append(item)
        check_addresses(inputs)
        check_addresses(outputs)
        output_words = [item for item in outputs if item.address is not None
                        and not item.address.coil and item.address.bit is None]
        input_words = [item for item in inputs if item.address is not None
                       and not item.address.coil and item.address.bit is None]
        output_bits = [item for item in outputs if item not in output_words]
        check_addresses(output_words, output_bits, shared_word=True)
        check_addresses(output_words, inputs, shared_word=True)
        check_addresses(input_words, output_bits, shared_word=True)
        check_addresses(output_bits, inputs)
        counts = self.integer(fields, "pulsos_pos", 4, 16383, "Pulsos entre estaciones")
        tolerance = self.integer(fields, "tolerancia", 0, (counts - 1) // 2, "Tolerancia horizontal")
        speed, minimum, maximum, interval = (
            nonnegative_number(fields[key], name) for key, name in (
                ("velocidad", "Velocidad directa"), ("vel_min", "Velocidad mínima"),
                ("vel_max", "Velocidad máxima"), ("intervalo", "Tiempo entre cajas"),
            )
        )
        if maximum <= minimum or (addresses["velocidad_mw"] is None and not minimum <= speed <= maximum):
            raise ValueError(f"Movimiento / Velocidad: mínima {minimum:g}, máxima {maximum:g}, directa {speed:g}.")
        if interval < .1:
            raise ValueError("El tiempo entre cajas debe ser de al menos 0,1 segundos.")
        low = self.integer(fields, "mw_min", 0, 65534, "Valor MW al mínimo")
        high = self.integer(fields, "mw_max", low + 1, 65535, "Valor MW al máximo")
        stock = self.integer(fields, "stock", 0, 10000, "Palets iniciales en la pila")
        gauge = None
        if fields["usar_barometro"] == "1":
            start, target, threshold, min_time, max_time = (
                nonnegative_number(fields[key], f"Barómetro / {label}")
                for key, label in (
                    ("presion_inicio", "Vacío inicial"), ("presion_final", "Vacío final"),
                    ("presion_umbral", "Umbral de agarre"), ("presion_min_s", "Tiempo mínimo"),
                    ("presion_max_s", "Tiempo máximo"),
                )
            )
            if not start < threshold <= target:
                raise ValueError(f"Barómetro: inicial {start:g}, umbral {threshold:g}, final {target:g} kPa. "
                                 "El umbral debe superar el vacío inicial y no exceder el final.")
            if min_time > max_time:
                raise ValueError(f"Barómetro: tiempo mínimo {min_time:g} s mayor que el máximo {max_time:g} s.")
            gauge = VacuumGauge(start, target, threshold, min_time, max_time)
        return PalletConfig(mode, addresses, counts, tolerance, speed, minimum, maximum, low, high, interval, stock, gauge)

    def toggle(self):
        if self.cancel is not None:
            self.pause()
            return
        fields = {key: var.get().strip() for key, var in self.fields.items()}
        try:
            config = self.configuration(fields)
            endpoint = self.endpoint() if any(config.addresses.values()) else None
        except ValueError as error:
            startup_error(self.parent, "el paletizador", self.status, error)
            return
        previous, self.saved = self.saved, fields
        if not self.save():
            self.saved = previous
            self.status.set("No se pudo guardar la configuración del paletizador.")
            return
        if self.config is None or config.stock != self.config.stock:
            self.state = replace(self.state, stock=config.stock)
        if config.gauge is not None and (self.config is None or config.gauge != self.config.gauge):
            self.state = replace(self.state, pressure=config.gauge.start, vacuum_elapsed=0.0,
                                 vacuum_duration=random.uniform(config.gauge.min_time, config.gauge.max_time))
        self.config, self.target = config, endpoint
        self.cancel = threading.Event()
        self.last_sample = None
        self.published = {}
        self.start_button.configure(text="Pausar")
        self.reset_button.configure(state="disabled")
        for edit, _ in self.edits:
            edit.configure(state="disabled")
        self.status.set("Paletizador en marcha.")
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
        self.reset_button.configure(state="normal")
        for edit, state in self.edits:
            edit.configure(state=state)
        ConveyorSimulator.update_signals(self, (False,) * len(SIGNALS))
        self.status.set("Paletizador en pausa.")
        self.draw()

    def reset_session(self):
        self.state = PalletState()
        self.config = None
        self.displays = (None,) * 3
        self.status.set("Configura el mando y pulsa Iniciar.")
        self.draw()

    def schedule(self, delay=TICK_MS):
        if self.cancel is not None and self.timer is None and not self.in_flight:
            self.timer = self.parent.after(delay, self.sample)

    def sample(self):
        self.timer = None
        self.in_flight = True
        cancel = self.cancel
        args = (cancel, self.config, self.target, self.state, self.last_sample,
                self.published.copy(), self.compatibility.get())
        threading.Thread(target=self.cycle, args=args, daemon=True).start()
        self.parent.after(15, self.poll, cancel)

    def cycle(self, cancel, config, endpoint, state, last, published, compatibility):
        readings, errors, cache = {}, [], {}

        def read(key):
            address = config.addresses[key]
            if address is None or cancel.is_set():
                return None
            try:
                if address not in cache:
                    cache[address] = self.exchange(*endpoint, address, cancelled=cancel)
                return cache[address]
            except (OSError, ProtocolError) as error:
                label = (self.words | self.bit_inputs | self.signal_inputs)[key]
                errors.append(f"{label}: {error}")
                return None

        control_keys = [*self.bit_inputs, *(key for key in self.words if not key.startswith("display"))]
        for key in control_keys:
            readings[key] = read(key)
        now = time.monotonic()
        valid = not any(config.addresses[key] is not None and readings[key] is None for key in control_keys)
        if valid:
            elapsed = min(.2, now - last) if last is not None else 0.0
            state = state.step(config, readings, elapsed)
        for key, value in state.outputs(config).items():
            if cancel.is_set():
                self.results.put(None)
                return
            address = config.addresses[key]
            if address is not None and published.get(key) != value:
                try:
                    self.exchange(*endpoint, address, value, compatibility, cancelled=cancel)
                    published[key] = value
                    cache[address] = value
                except (OSError, ProtocolError) as error:
                    errors.append(f"{self.outputs[key]}: {error}")
        signals = tuple(bool(read(key)) for key in self.signal_inputs)
        displays = tuple(read(f"display{index}") for index in range(3))
        self.results.put((state, now if valid else None, published, signals, displays, " · ".join(errors)))

    def poll(self, cancel):
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.parent.after(15, self.poll, cancel)
            return
        self.in_flight = False
        if self.cancel is cancel and result is not None:
            self.state, self.last_sample, self.published, signals, self.displays, error = result
            ConveyorSimulator.update_signals(self, signals)
            self.status.set(error or self.state.event or "Paletizador en marcha.")
            self.draw()
        self.schedule(0 if self.cancel is not cancel else TICK_MS)

    def draw(self):
        canvas = self.diagram
        canvas.delete("all")
        width = max(780, canvas.winfo_width())
        height = max(220, canvas.winfo_height())
        state = self.state
        left, spacing = width * .19, width * .14
        xs = [left + spacing * index for index in range(5)]
        deck = height * .70
        rail = height * .19
        canvas.create_line(left, rail, xs[4], rail, fill="#567588", width=7)
        conveyor_start = left - width * .15
        travel = left - conveyor_start - 27
        stop_x = left + (BELT_STOP - 1) * travel + 26
        canvas.create_rectangle(conveyor_start, deck - 12, stop_x, deck + 7,
                                fill="#456178", outline="")
        canvas.create_line(stop_x, deck - 32, stop_x, deck + 8, fill="#db694c", width=4, tags=("tope",))
        canvas.create_text(stop_x, deck + 19, text="Tope", fill="#a74732", tags=("tope",))
        canvas.create_line(left, deck - 37, left, deck + 8, dash=(3, 3), fill="#17688b")
        if self.config is not None:
            x = conveyor_start + 27 + travel * state.feed
            self.box(canvas, x, deck - 12)
            direction = int("cinta_adelante" in state.active) - int("cinta_atras" in state.active)
            if direction:
                a, b = conveyor_start + 10, left - 22
                if direction < 0:
                    a, b = b, a
                canvas.create_line(a, deck - 42, b, deck - 42, arrow="last", fill="#17688b", width=2)
        for index, name in enumerate(POSITIONS):
            x = xs[index]
            reach = self.config.reach if self.config is not None else .015
            detected = abs(state.position - index) <= reach
            canvas.create_oval(x - 5, rail - 4, x + 5, rail + 6,
                               fill="#35c8e7" if detected else "#b6c6ce", outline="#356175",
                               tags=("sensor_posicion_robot", f"sensor_posicion_{index}"))
            canvas.create_text(x, rail - 10, anchor="s", text=("Inicial", "1", "2", "3", "Pila")[index],
                               fill="#2f5066", tags=("sensor_posicion_robot",))
            canvas.create_text(x, deck + 22, text=name, fill="#243746", font=("Segoe UI", 9, "bold"))
            if index in (1, 2, 3):
                slot = index - 1
                count = state.stations[slot]
                canvas.create_rectangle(x - 33, deck - 22, x + 33, deck + 5, outline="#8295a6", dash=(4, 3))
                if count >= 0:
                    self.pallet(canvas, x, deck)
                for level in range(min(3, max(0, count))):
                    self.box(canvas, x, deck - 7 - level * 10)
                for delta, fill, active in ((-9, "#17974f", count >= 0), (9, "#c23d36", count < 0)):
                    canvas.create_oval(x + delta - 5, deck + 34, x + delta + 5, deck + 44,
                                       fill=fill if active else "#dce5ed", outline=fill)
                name = self.fields[f"nombre{slot}"].get().strip() or "Cajas"
                value = self.displays[slot] if self.fields[f"display{slot}"].get().strip() else max(0, count)
                canvas.create_text(x, deck + 57, text=f"{name}: {value if value is not None else '—'}", fill="#243746")
            elif index == 4:
                self.draw_stock(canvas, x, deck, rail, state.stock)
                canvas.create_text(x, deck + 40, text=f"{state.stock} palets en pila",
                                   fill="#243746", tags=("cantidad_palets",))
        x = left + state.position * spacing
        jaw = rail + 38 + state.vertical * (deck - 23 - (rail + 38))
        canvas.create_line(x, rail, x, jaw, fill="#335468", width=5)
        canvas.create_rectangle(x - 12, rail - 10, x + 12, rail + 10, fill="#29475c", outline="")
        gauge_ready = self.config is not None and bool(state.outputs(self.config)["barometro_ok"])
        suction = state.vacuum and (self.config is None or self.config.gauge is None or gauge_ready)
        tank_color = "#6bb0bf" if suction else "#d49b48" if state.vacuum else "#9eacb7"
        canvas.create_oval(x - 25, jaw - 26, x + 25, jaw - 11, fill=tank_color,
                           outline="#335468", width=2, tags=("calderin",))
        canvas.create_text(x + 29, jaw - 19, anchor="w", text="Vacío", fill="#365568", tags=("calderin",))
        canvas.create_line(x, jaw - 11, x, jaw - 5, x - 14, jaw - 5, x + 14, jaw - 5,
                           fill="#335468", width=2, tags=("calderin",))
        for offset in (-13, 13):
            canvas.create_oval(x + offset - 5, jaw - 3, x + offset + 5, jaw + 3,
                               fill="#1aa96c" if suction else "#788a97", outline="")
        if state.cargo == "Caja":
            self.box(canvas, x, jaw + 20)
        elif state.cargo == "Palet":
            self.pallet(canvas, x, jaw + 8)
        if self.config is not None:
            pulses = round(state.position * self.config.counts_per_position)
            vertical = round(state.vertical * self.config.counts_per_position)
            canvas.create_text(conveyor_start, 13, anchor="w",
                               text=f"Encoder X: {pulses} · Encoder Z: {vertical} pulsos · Roturas: {state.broken}",
                               fill="#243746")
        for index, ((key, name, color), active) in enumerate(zip(SIGNALS, self.signals)):
            x, y = width * .86, 17 + index * 23
            ConveyorSimulator.draw_signal(canvas, key, x, y, color, active)
            canvas.create_text(x + 15, y, text=name, anchor="w", fill="#243746")
        reach = self.config.reach if self.config is not None else .015
        for label, active, y in (("Brazo arriba", state.vertical <= reach, 162),
                                 ("Brazo abajo", 1 - state.vertical <= reach, 184)):
            x = width * .86
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#35c8e7" if active else "#b6c6ce",
                               outline="#356175", tags=("sensor_vertical_robot",))
            canvas.create_text(x + 15, y, anchor="w", text=label, fill="#243746",
                               tags=("sensor_vertical_robot",))
        if self.config is not None and self.config.gauge is not None:
            x = width * .86
            label = "Presión alcanzada" if gauge_ready else "Esperando presión" if state.vacuum else "Vacío apagado"
            canvas.create_text(x - 10, 205, anchor="w", text=f"{state.pressure:.1f} kPa",
                               fill="#17694d" if gauge_ready else "#614938", tags=("barometro",))
            canvas.create_text(x - 10, 224, anchor="w", text=label, fill="#243746", tags=("barometro",))

    @staticmethod
    def draw_stock(canvas, x, bottom, rail, count):
        if not count:
            return
        height = min(count * 7, bottom - rail - 12)
        top = bottom - height
        canvas.create_rectangle(x - 28, top, x + 28, bottom, fill="#aa7441", outline="#725035",
                                tags=("pila_palets",))
        if count > 1:
            points = []
            for level in range(1, count):
                y = top + height * level / count
                sides = (x - 28, x + 28) if level % 2 else (x + 28, x - 28)
                points.extend((sides[0], y, sides[1], y))
            canvas.create_line(*points, fill="#725035", tags=("pila_palets",))

    @staticmethod
    def box(canvas, x, bottom):
        canvas.create_rectangle(x - 26, bottom - 20, x + 26, bottom,
                                fill="#dfaa6f", outline="#8a572d", tags=("caja_paletizador",))

    @staticmethod
    def pallet(canvas, x, bottom):
        canvas.create_rectangle(x - 28, bottom - 6, x + 28, bottom,
                                fill="#aa7441", outline="#725035", tags=("palet",))
