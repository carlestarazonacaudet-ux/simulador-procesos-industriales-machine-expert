"""Cinta reversible con cajas y tres sensores de presencia."""

import queue
import random
import sys
from math import atan2, degrees, hypot, isfinite, isqrt, sqrt
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import font, messagebox, ttk

if sys.platform == "win32":
    import winsound

from modbus import Address, ProtocolError, parse_address
from simulation import AddressField, check_addresses, configured, nonnegative_number, startup_error

STATIONS = (("Entrada", 0.04), ("Centro", 0.50), ("Final", 0.96))
SIZES = ("Pequeñas", "Grandes")
METAL_SIZES = ("Pequeñas", "Nominales", "Grandes")
PROCESS_METHODS = ("Alturas fijas", "Normal (sigma)", "Normal (nivel sigma)", "Buenas por cada 100")
SENSORS = tuple((name, position, size) for size in SIZES for name, position in STATIONS)
BOX_LENGTH = {"Pequeñas": 0.10, "Nominales": 0.14, "Grandes": 0.19}
MAX_TILE_LENGTH = 0.14
TILE_SHAPES = {
    "Cuadradas": ((0, -.50, .50), (1, -.50, .50)),
    "Trapezoidales": ((0, -.30, .40), (1, -.60, .60)),
    "Irregulares": ((0, -.42, .44), (.25, -.57, .45), (.51, -.51, .51),
                    (.76, -.28, .60), (1, -.47, .38)),
}
MIXED_SHAPES = "Trapezoidales e irregulares"
SIGNALS = (
    ("azul", "Azul", "#2383ef"), ("verde", "Verde", "#20bd62"),
    ("amarillo", "Amarillo", "#f0bf29"), ("rojo", "Rojo", "#e14741"),
    ("sirena", "Sirena", "#e09d36"), ("luz_emergencia", "Luz de emergencia", "#fc5049"),
)
METERS = (
    ("temporizador1", "Display 1"), ("temporizador2", "Display 2"),
    ("contador1", "Display 3"), ("contador2", "Display 4"),
    ("display5", "Display 5"), ("display6", "Display 6"),
)
SAMPLE_MS = 85
BUTTONS = (
    ("marcha", "Marcha", "#238250"),
    ("paro", "Paro", "#b7473a"),
    ("emergencia", "Emergencia", "#bd282c"),
    ("reset", "Reset", "#266699"),
)


@dataclass(frozen=True)
class Box:
    center: float
    size: str
    height_mm: float | None = None
    length: float | None = None
    profile: tuple = ()
    offset_mm: float = 0.0

    @property
    def half(self):
        return (self.length if self.length is not None else BOX_LENGTH[self.size]) / 2

    def edges(self, position):
        fraction = min(1.0, max(0.0, (position - self.center) / (2 * self.half) + .5))
        previous, following = next(
            (a, b) for a, b in zip(self.profile, self.profile[1:]) if fraction <= b[0]
        )
        weight = (fraction - previous[0]) / (following[0] - previous[0])
        return tuple(self.offset_mm + self.height_mm * (a + (b - a) * weight)
                     for a, b in zip(previous[1:], following[1:]))


@dataclass(frozen=True)
class HeightStats:
    count: int = 0
    mean: float = 0.0
    squared_deviations: float = 0.0
    good: int = 0
    low: int = 0

    def add(self, height, lower, upper):
        count = self.count + 1
        delta = height - self.mean
        mean = self.mean + delta / count
        return HeightStats(count, mean, self.squared_deviations + delta * (height - mean),
                           self.good + int(lower <= height <= upper), self.low + int(height < lower))

    @property
    def variance(self):
        return max(0.0, self.squared_deviations / (self.count - 1)) if self.count > 1 else None


@dataclass(frozen=True)
class HeightProcess:
    method: str
    heights: tuple[float, float, float]
    tolerance: float
    mean: float
    sigma: float
    good_per_batch: int

    @property
    def limits(self):
        nominal = self.heights[1]
        return nominal - self.tolerance, nominal + self.tolerance

    def generate(self, size, emitted, batch):
        minimum, _, maximum = self.heights
        lower, upper = self.limits
        if self.method == "Alturas fijas":
            return self.heights[METAL_SIZES.index(size)], batch
        if self.method != "Buenas por cada 100":
            return max(minimum, min(maximum, random.gauss(self.mean, self.sigma))), batch
        position = emitted % 100
        if position == 0 or batch is None:
            batch = frozenset(random.sample(range(100), self.good_per_batch))
        if position in batch:
            return random.uniform(max(minimum, lower), min(maximum, upper)), batch
        if random.choice((False, True)):
            return random.uniform(minimum, (minimum + lower) / 2), batch
        return random.uniform((upper + maximum) / 2, maximum), batch


@dataclass(frozen=True)
class Belt:
    boxes: tuple[Box, ...] = ()
    feed_in: float = 0.0
    emitted: int = 0
    next_size: str | None = None
    batch: frozenset[int] | None = None
    stats: HeightStats = HeightStats()

    def advance(self, direction, speed, interval, seconds, size, limit, sizes=SIZES, process=None, *, tile_geometry=None):
        if direction == 0:
            return self
        movement = direction * speed * seconds
        boxes = tuple(replace(box, center=center) for box in self.boxes
                      if -box.half <= (center := box.center + movement) <= 1 + box.half)
        feed_in = max(0.0, self.feed_in - seconds)
        emitted, next_size = self.emitted, self.next_size
        batch, stats = self.batch, self.stats
        if feed_in == 0 and (limit is None or emitted < limit):
            next_size = (next_size or random.choice(sizes)) if size == "Aleatorias" else size
            half = (MAX_TILE_LENGTH if tile_geometry is not None else BOX_LENGTH[next_size]) / 2
            entrance = 0.0 if direction > 0 else 1.0
            trial_center = entrance - direction * half
            if all(abs(box.center - trial_center) >= box.half + half + 0.03 for box in boxes):
                measured = None
                length = None
                profile, offset = (), 0.0
                if process is not None:
                    measured, batch = process.generate(next_size, emitted, batch)
                    stats = stats.add(measured, *process.limits)
                    if tile_geometry is not None:
                        length = MAX_TILE_LENGTH * measured / process.heights[2]
                        half = length / 2
                        shape = tile_geometry.shape
                        if shape == MIXED_SHAPES:
                            shape = random.choice(("Trapezoidales", "Irregulares"))
                        profile = TILE_SHAPES[shape]
                        offset = random.uniform(*tile_geometry.offsets)
                center = entrance - direction * half
                boxes = (*boxes, Box(center, next_size, measured, length, profile, offset))
                emitted += 1
                next_size = None
                feed_in = interval
        return Belt(boxes, feed_in, emitted, next_size, batch, stats)

    def detect(self):
        return tuple(
            int(any((size == "Pequeñas" or box.size == "Grandes")
                    and abs(box.center - position) <= box.half for box in self.boxes))
            for _, position, size in SENSORS
        )


@dataclass(frozen=True)
class TileGeometry:
    shape: str
    offsets: tuple[float, float]
    gap_full_scale: float
    sensor_addresses: tuple[Address | None, ...]
    reference_half: float
    corners_mode: bool = False

    def piece(self, belt):
        return next((box for box in belt.boxes if abs(box.center - .5) <= box.half), None)

    def gaps(self, belt):
        piece = self.piece(belt)
        if piece is None:
            return None
        upper, lower = piece.edges(.5)
        return self.reference_half + upper, self.reference_half - lower

    def sensor_word(self, gap, full_word):
        return round(full_word * min(1.0, gap / self.gap_full_scale))

    def inspect_corners(self, belt):
        piece = self.piece(belt)
        if piece is None:
            return None
        _, upper_left, lower_left = piece.profile[0]
        _, upper_right, lower_right = piece.profile[-1]
        size = piece.height_mm
        points = (
            (0.0, piece.offset_mm + upper_left * size),
            (size, piece.offset_mm + upper_right * size),
            (size, piece.offset_mm + lower_right * size),
            (0.0, piece.offset_mm + lower_left * size),
        )
        gaps = tuple(self.reference_half + y if index < 2 else self.reference_half - y
                     for index, (_, y) in enumerate(points))
        return CornerMeasurement(points, gaps)


@dataclass(frozen=True)
class CornerMeasurement:
    points: tuple[tuple[float, float], ...]
    gaps: tuple[float, ...]

    def distance(self, first, second):
        ax, ay = self.points[first]
        bx, by = self.points[second]
        return hypot(bx - ax, by - ay)

    @property
    def widths(self):
        return self.distance(0, 3), self.distance(1, 2)

    @property
    def lengths(self):
        return self.distance(0, 1), self.distance(3, 2)

    @property
    def offset(self):
        return sum(y for _, y in self.points) / 4

    @property
    def diagonal_difference(self):
        return abs(self.distance(0, 2) - self.distance(1, 3))

    @property
    def angle_error(self):
        errors = []
        for index, (x, y) in enumerate(self.points):
            px, py = self.points[index - 1]
            nx, ny = self.points[(index + 1) % 4]
            ax, ay, bx, by = px - x, py - y, nx - x, ny - y
            angle = degrees(atan2(abs(ax * by - ay * bx), ax * bx + ay * by))
            errors.append(abs(angle - 90))
        return max(errors)


@dataclass(frozen=True)
class HeightSensor:
    address: Address | None
    heights: tuple[float, float, float]
    tolerance: float
    full_height: float
    full_word: int
    process: HeightProcess | None = None
    tile_geometry: TileGeometry | None = None

    def height(self, box):
        return box.height_mm if box.height_mm is not None else self.heights[METAL_SIZES.index(box.size)]

    def measure(self, belt):
        piece = next((box for box in belt.boxes if abs(box.center - 0.5) <= box.half), None)
        if piece is None:
            return 0.0, 0.0, 0, "Sin pieza"
        if self.tile_geometry is None:
            height = self.height(piece)
        elif self.tile_geometry.corners_mode:
            measurement = self.tile_geometry.inspect_corners(belt)
            height = round(sum(measurement.widths) / 2, 10)
        else:
            gaps = self.tile_geometry.gaps(belt)
            height = round(2 * self.tile_geometry.reference_half - sum(gaps), 10)
        fraction = min(1.0, height / self.full_height)
        difference = height - self.heights[1]
        classification = ("Correcta" if abs(difference) <= self.tolerance
                          else "Más alta" if difference > 0 else "Más baja")
        return height, 10 * fraction, round(self.full_word * fraction), classification


@dataclass(frozen=True)
class BeltConfig:
    right: Address | None
    left: Address | None
    drive: Address | None
    minimum: int
    maximum: int
    sensors: tuple[Address | None, ...]
    signals: tuple[Address | None, ...]
    meters: tuple[Address | None, ...]
    meter_names: tuple[str, ...]
    speed: float
    interval: float
    size: str
    limit: int | None
    height_sensor: HeightSensor | None = None

    def drive_speed(self, word):
        value = word - 65536 if word >= 32768 else word
        if value < 0:
            return -1, min(1.0, value / self.minimum), value
        if value > 0:
            return 1, min(1.0, value / self.maximum), value
        return 0, 0.0, value


class ConveyorSimulator:
    def __init__(self, parent, endpoint, exchange, save, compatibility, bind_pulse, press_pulse,
                 write_selector, *, height_mode=False, tile_mode=False, corner_mode=False):
        self.parent = parent
        self.endpoint = endpoint
        self.exchange = exchange
        self.save = save
        self.compatibility = compatibility
        self.press_pulse = press_pulse
        self.write_selector = write_selector
        self.corner_mode = corner_mode
        tile_mode = tile_mode or corner_mode
        self.tile_mode = tile_mode
        height_mode = height_mode or tile_mode
        self.height_mode = height_mode
        self.height_sensor = None
        self.fields = {}
        self.button_fields = {}
        self.button_images = []
        self.editors = []
        self.choices = []
        self.saved = {}
        self.belt = Belt()
        self.direction = 0
        self.speed_ratio = 0.0
        self.results = queue.Queue()
        self.cancel = None
        self.timer = None
        self.in_flight = False
        self.last_sample = None
        self.published = {}
        self.signals = (False,) * len(SIGNALS)
        self.meters = (None,) * len(METERS)
        self.siren_playing = False
        self.status = tk.StringVar(value=self.initial_status())
        self.button_status = tk.StringVar(value="Mantén pulsado para enviar el estado elegido.")

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        drive = ttk.LabelFrame(controls, text="Movimiento y entrada de azulejos" if tile_mode
                               else "Movimiento y entrada de cajas", padding=8)
        drive.pack(side="left", anchor="n", padx=(0, 12))
        drive.columnconfigure(0, weight=1)
        drive.columnconfigure(1, minsize=132)
        mode = tk.StringVar(value="Dos bits")
        self.fields["modo"] = mode
        ttk.Label(drive, text="Modo de mando").grid(row=0, column=0, sticky="w")
        self.mode_picker = ttk.Combobox(
            drive, textvariable=mode, values=("Dos bits", "Variador %MW"), state="readonly", width=14,
        )
        self.mode_picker.grid(row=0, column=1, sticky="ew", padx=3)
        self.mode_picker.bind("<<ComboboxSelected>>", lambda event: self.show_mode())
        if height_mode:
            drive.grid_slaves(row=0, column=0)[0].grid_remove()
            self.mode_picker.grid_remove()
        self.bit_panel = ttk.Frame(drive)
        self.bit_panel.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.bit_panel.columnconfigure(0, weight=1)
        self.bit_panel.columnconfigure(1, minsize=132)
        self.field(self.bit_panel, "derecha", "Marcha adelante" if height_mode else "Marcha derecha", "", 0, align="ew")
        self.field(self.bit_panel, "izquierda", "Marcha atrás" if height_mode else "Marcha izquierda", "", 1, align="ew")
        self.drive_panel = ttk.Frame(drive)
        self.drive_panel.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.drive_panel.columnconfigure(0, weight=1)
        self.drive_panel.columnconfigure(1, minsize=132)
        self.field(self.drive_panel, "variador", "Consigna %MW", "", 0, align="ew")
        self.field(self.drive_panel, "minimo", "Izquierda máx.", "-100", 1, align="ew")
        self.field(self.drive_panel, "maximo", "Derecha máx.", "100", 2, align="ew")
        ttk.Label(self.drive_panel, text="0 = parada").grid(row=3, columnspan=2, sticky="w")
        self.field(drive, "velocidad", "Velocidad % de la cinta", "25", 2, align="ew")
        self.field(drive, "intervalo", "Tiempo entre piezas (s)" if height_mode else "Tiempo entre cajas (s)",
                   "1.5", 3, align="ew")
        sizes = ("Aleatorias", *METAL_SIZES) if height_mode else (*SIZES, "Aleatorias")
        self.choice(drive, "tamano", "Piezas" if height_mode else "Cajas", sizes, 4)
        self.choice(drive, "alimentacion", "Alimentación", ("Sinfín", "Cantidad fija"), 5)
        self.field(drive, "cantidad", "Cantidad fija", "10", 6, align="ew")

        connections = ttk.Notebook(controls)
        connections.pack(side="left", anchor="n")
        sensors = ttk.Frame(connections, padding=5)
        lights = ttk.Frame(connections, padding=5)
        meters = ttk.Frame(connections, padding=5)
        connections.add(sensors, text="Sensores de ancho" if tile_mode else "Sensor difuso" if height_mode else "Sensores")
        connections.add(lights, text="Avisos")
        connections.add(meters, text="Displays")
        if height_mode:
            for index, (key, label, default) in enumerate((
                ("salida_analogica", "Salida analógica %MW", ""),
                ("altura_baja", "Ancho pequeño (mm)" if tile_mode else "Pieza baja (mm)", "20"),
                ("altura_nominal", "Ancho nominal (mm)" if tile_mode else "Altura nominal (mm)", "40"),
                ("altura_alta", "Ancho grande (mm)" if tile_mode else "Pieza alta (mm)", "60"),
                ("tolerancia", "Tolerancia ± (mm)", "5"),
                ("altura_10v", "Ancho a 10 V (mm)" if tile_mode else "Altura a 10 V (mm)", "80"),
                ("cuentas_10v", "Valor %MW a 10 V", "10000"),
            )):
                self.field(sensors, key, label, default, index, width=10)
            process = ttk.Frame(connections, padding=5)
            connections.insert(1, process, text="Proceso")
            self.choice(process, "metodo_altura", "Método", PROCESS_METHODS, 0)
            self.field(process, "media_altura", "Media (mm, opcional)", "", 1, width=12)
            self.field(process, "sigma_altura", "Sigma (mm)", "1", 2, width=12)
            self.field(process, "nivel_sigma", "Nivel sigma", "6", 3, width=12)
            self.field(process, "buenas_100", "Buenas de cada 100", "95", 4, width=12)
            self.choices[-1].configure(width=22)
            self.choices[-1].bind("<<ComboboxSelected>>", lambda event: self.show_process_fields())
            self.process_rows = {row: process.grid_slaves(row=row) for row in range(1, 5)}
            self.show_process_fields()
            if tile_mode:
                geometry = ttk.Frame(connections, padding=5)
                connections.insert(2, geometry, text="Forma y posición")
                self.choice(geometry, "forma_azulejo", "Forma", (*TILE_SHAPES, MIXED_SHAPES), 0)
                self.choices[-1].configure(width=23)
                for row, (key, label, default) in enumerate((
                    ("offset_min", "Desplaz. mínimo (mm)", "0"),
                    ("offset_max", "Desplaz. máximo (mm)", "0"),
                    ("distancia_10v", "Distancia a 10 V (mm)", "80"),
                ), 1):
                    self.field(geometry, key, label, default, row, width=11)
                for row, (key, label) in enumerate(self.tile_channels(), 4):
                    self.field(geometry, key, f"{label} %MW", "", row, width=11)
                ttk.Label(geometry, text="Negativo ↑   Positivo ↓").grid(row=8, columnspan=2, sticky="w")
        else:
            for column, name in enumerate(("Posición", "Pequeñas", "Grandes")):
                ttk.Label(sensors, text=name).grid(row=0, column=column, padx=3)
            for index, (name, _) in enumerate(STATIONS):
                ttk.Label(sensors, text=name).grid(row=index + 1, column=0, sticky="w")
                for size_index in range(2):
                    self.field(sensors, f"sensor{size_index * 3 + index}", "", "", index + 1,
                               column=size_index + 1, width=10)
        for index, (key, name, _) in enumerate(SIGNALS):
            self.field(lights, key, name, "", index, width=11)
        ttk.Label(meters, text="Dirección").grid(row=0, column=1, sticky="w")
        ttk.Label(meters, text="Nombre").grid(row=0, column=2, sticky="w")
        for index, (key, name) in enumerate(METERS, 1):
            self.field(meters, key, name, "", index, width=11)
            self.field(meters, f"{key}_nombre", "", "", index, column=2, width=17)
        self.make_buttons(controls, bind_pulse)

        feedback = ttk.Frame(parent, height=38)
        feedback.pack(fill="x")
        feedback.pack_propagate(False)
        message = ttk.Label(feedback, textvariable=self.button_status, anchor="nw", justify="left")
        message.pack(fill="both", expand=True)
        feedback.bind("<Configure>", lambda event: message.configure(wraplength=event.width))

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(0, 6))
        self.start_button = ttk.Button(actions, text="Iniciar", command=self.toggle)
        self.start_button.pack(side="left")
        self.reset_button = ttk.Button(actions, text="Retirar piezas" if height_mode else "Retirar cajas", command=self.reset)
        self.reset_button.pack(side="left", padx=8)
        self.motion = tk.StringVar()
        ttk.Label(actions, textvariable=self.motion, font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
        self.counter = tk.StringVar()
        ttk.Label(actions, textvariable=self.counter).pack(side="left", padx=8)
        if height_mode:
            ttk.Button(actions, text="Calcular variabilidad", command=self.show_variability).pack(side="right")
        if self.corner_mode:
            self.corner_summary = tk.StringVar()
            ttk.Label(parent, textvariable=self.corner_summary, wraplength=1000).pack(fill="x", pady=(0, 5))
        self.diagram = tk.Canvas(parent, background="#f1f5f9", highlightthickness=0, height=196)
        self.meter_font = font.Font(root=parent, family="Segoe UI", size=8)
        self.diagram.pack(fill="both", expand=True)
        self.diagram.bind("<Configure>", lambda event: self.draw())
        for key, _ in METERS:
            self.fields[f"{key}_nombre"].trace_add("write", lambda *_: self.draw())
        ttk.Label(parent, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=(6, 0))
        self.show_mode()
        self.draw()

    def initial_status(self):
        if self.corner_mode:
            return "Configura los bits de marcha y las palabras de los cuatro sensores de esquina."
        if self.tile_mode:
            return "Configura los bits de marcha y la palabra de salida de los sensores de ancho."
        if self.height_mode:
            return "Configura los bits de marcha y la palabra de salida del sensor de altura."
        return "Selecciona el modo de mando y configura las direcciones."

    def tile_channels(self):
        if self.corner_mode:
            return (("esquina_si", "S1 Superior izquierda"), ("esquina_sd", "S2 Superior derecha"),
                    ("esquina_id", "S3 Inferior derecha"), ("esquina_ii", "S4 Inferior izquierda"))
        return (("sensor_superior", "Sensor superior"), ("sensor_inferior", "Sensor inferior"))

    def make_buttons(self, controls, bind_pulse):
        panel = ttk.LabelFrame(controls, text="Pulsadores de la cinta", padding=8)
        panel.pack(side="left", anchor="n", padx=(12, 0))
        for column, heading in enumerate(("Pulsador", "Bit", "Símbolo", "Al pulsar")):
            ttk.Label(panel, text=heading).grid(row=0, column=column, sticky="w", padx=3)
        for row, (key, label, color) in enumerate(BUTTONS, start=1):
            image = tk.PhotoImage(master=panel, width=19, height=19)
            for radius, fill in ((9, "#3e4a53"), (7, color), (3, "#f0e4dd")):
                center_y = 7 if radius == 3 else 9
                for offset in range(-radius, radius + 1):
                    half = isqrt(radius * radius - offset * offset)
                    y = center_y + offset
                    image.put(fill, to=(9 - half, y, 10 + half, y + 1))
            self.button_images.append(image)
            style = ttk.Style(panel)
            style.configure("ConveyorPulse.TButton", anchor="w")
            button = ttk.Button(panel, text=label, image=image, compound="left", width=11,
                                style="ConveyorPulse.TButton")
            button.grid(row=row, column=0, sticky="ew", padx=3, pady=2)
            for column, suffix, default in ((1, "bit", ""), (2, "simbolo", ""), (3, "estado", "Activo")):
                field_key = f"{key}_{suffix}"
                variable = tk.StringVar(value=default)
                self.fields[field_key] = self.button_fields[field_key] = variable
                if suffix == "estado":
                    editor = ttk.Combobox(panel, state="readonly", values=("Activo", "Inactivo"),
                                          textvariable=variable, width=8)
                    editor.bind("<<ComboboxSelected>>", lambda event: self.save_buttons())
                else:
                    editor = ttk.Entry(panel, textvariable=variable, width=12)
                    editor.bind("<Return>", lambda event: self.save_buttons())
                    editor.bind("<FocusOut>", lambda event: self.save_buttons())
                editor.grid(row=row, column=column, sticky="w", padx=3)
            bind_pulse(button, lambda key=key, label=label, button=button: self.press_button(key, label, button))
        selector_panel = ttk.Frame(panel)
        selector_panel.grid(row=5, column=0, rowspan=2, sticky="w", padx=3, pady=(6, 0))
        ttk.Label(selector_panel, text="Selector").pack(side="left")
        self.selector_dial = tk.Canvas(selector_panel, width=45, height=44, highlightthickness=0,
                                       cursor="hand2", takefocus=True,
                                       background=ttk.Style(panel).lookup("TFrame", "background") or "#f0f0f0")
        self.selector_dial.pack(side="left", padx=(3, 0))
        for event in ("<ButtonRelease-1>", "<Return>", "<space>"):
            self.selector_dial.bind(event, self.toggle_selector)
        for index in (1, 2):
            for column, key in ((1, f"selector_bit{index}"), (2, f"selector_simbolo{index}")):
                variable = tk.StringVar()
                self.fields[key] = self.button_fields[key] = variable
                editor = ttk.Entry(panel, textvariable=variable, width=12)
                editor.grid(row=4 + index, column=column, sticky="w", padx=3, pady=2)
                editor.bind("<Return>", lambda event: self.save_buttons())
                editor.bind("<FocusOut>", lambda event: self.save_buttons())
            ttk.Label(panel, text=f"Pos. {index}").grid(row=4 + index, column=3, sticky="w", padx=3)
        self.selector = ttk.Combobox(panel, values=("Pos. 1", "Pos. 2"), width=8, state="readonly")
        self.selector.grid(row=7, column=3, sticky="w", padx=3, pady=(4, 0))
        self.selector.set("Elegir…")
        self.selector.bind("<<ComboboxSelected>>", lambda event: self.change_selector())
        self.draw_selector()

    def toggle_selector(self, event=None):
        if "disabled" not in self.selector.state():
            self.selector.current(1 if self.selector.current() == 0 else 0)
            self.change_selector()
        return "break"

    def draw_selector(self, position=None):
        dial = self.selector_dial
        dial.delete("all")
        dial.create_oval(8, 12, 37, 41, fill="#324555", outline="#617787", width=2)
        for index, x in enumerate((8, 37)):
            color = "#167642" if position == index else "#526575"
            dial.create_text(x, 7, text=str(index + 1), fill=color, font=("Segoe UI", 8, "bold"))
        if position is None:
            dial.create_oval(20, 24, 25, 29, fill="#b0bfca", outline="")
        else:
            x = 14 if position == 0 else 31
            dial.create_line(22.5, 27, x, 17, fill="#a9f2c2", width=4, capstyle="round", tags=("aguja",))

    def change_selector(self):
        position = self.selector.current()
        if position < 0:
            return
        try:
            addresses = tuple(self.bit(self.button_fields[f"selector_bit{index}"].get().strip(),
                                       f"Selector, posición {index}") for index in (1, 2))
            if None in addresses or addresses[0] == addresses[1]:
                raise ValueError("Configura dos bits distintos para el selector.")
            if self.cancel is not None:
                for address in addresses:
                    self.check_button_output(address, self.config.sensors, self.control_words(), "Selector")
        except ValueError as error:
            self.button_status.set(str(error))
            self.selector.set("Elegir…")
            return
        if not self.save_buttons():
            self.selector.set("Elegir…")
            return
        self.selector.configure(state="disabled")

        def complete(success):
            self.selector.configure(state="readonly")
            if success:
                self.draw_selector(position)
                symbol = self.button_fields[f"selector_simbolo{position + 1}"].get().strip()
                self.button_status.set(f"Selector: {symbol or f'posición {position + 1}'}.")
            else:
                self.selector.set("Elegir…")
                self.draw_selector()

        self.write_selector(addresses[1 - position], addresses[position], self.button_status, complete)

    def save_buttons(self):
        values = {key: variable.get().strip() for key, variable in self.button_fields.items()}
        if all(self.saved.get(key, "Activo" if key.endswith("_estado") else "") == value
               for key, value in values.items()):
            return True
        previous, self.saved = self.saved, {**self.saved, **values}
        if self.save():
            return True
        self.saved = previous
        self.button_status.set("No se pudo guardar la configuración de los pulsadores.")
        return False

    def press_button(self, key, label, button):
        try:
            raw = self.button_fields[f"{key}_bit"].get().strip()
            address = self.bit(raw, label)
            if address is None:
                raise ValueError(f"Configura el bit de {label}.")
            if self.cancel is not None:
                self.check_button_output(address, self.config.sensors, self.control_words(), label)
        except ValueError as error:
            self.button_status.set(str(error))
            return "break"
        if not self.save_buttons():
            return "break"
        symbol = self.button_fields[f"{key}_simbolo"].get().strip()
        active = self.button_fields[f"{key}_estado"].get() == "Activo"
        return self.press_pulse(symbol or f"{label} ({raw})", address, int(active), button, self.button_status)

    @staticmethod
    def check_button_output(address, sensors, words, label):
        if address in sensors:
            raise ValueError(f"{label}: el bit está asignado a un sensor de la cinta.")
        if not address.coil and any(word is not None and address.index == word.index for word in words):
            raise ValueError(f"{label}: el bit pertenece a una palabra reservada por el simulador.")

    def control_words(self):
        if self.height_sensor is None:
            return (self.config.drive,)
        geometry = self.height_sensor.tile_geometry
        return (self.height_sensor.address, *(geometry.sensor_addresses if geometry is not None else ()))

    def field(self, panel, key, name, default, row, column=1, width=14, align="w"):
        variable = tk.StringVar(value=default)
        self.fields[key] = variable
        if name:
            ttk.Label(panel, text=name).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
        editor = ttk.Entry(panel, textvariable=variable, width=width)
        editor.grid(row=row, column=column, sticky=align, padx=3, pady=2)
        self.editors.append(editor)

    def choice(self, panel, key, name, options, row):
        variable = tk.StringVar(value=options[0])
        self.fields[key] = variable
        ttk.Label(panel, text=name).grid(row=row, column=0, sticky="w")
        editor = ttk.Combobox(panel, textvariable=variable, values=options, width=13, state="readonly")
        editor.grid(row=row, column=1, sticky="ew", padx=3)
        self.choices.append(editor)

    def load(self, settings):
        if isinstance(settings, dict):
            self.saved = {key: settings[key] for key in self.fields if isinstance(settings.get(key), str)}
            if "derecha" not in self.saved and isinstance(settings.get("sentido"), str):
                self.saved["derecha"] = settings["sentido"]
            for key, value in self.saved.items():
                self.fields[key].set(value)
            self.show_mode()
            if self.height_mode:
                self.show_process_fields()

    def show_process_fields(self):
        mode = self.fields["metodo_altura"].get()
        rows = {
            "Alturas fijas": (), "Normal (sigma)": (1, 2),
            "Normal (nivel sigma)": (1, 3), "Buenas por cada 100": (4,),
        }.get(mode, ())
        for row, widgets in self.process_rows.items():
            for widget in widgets:
                if row in rows:
                    widget.grid()
                else:
                    widget.grid_remove()

    def show_variability(self):
        title = "Variabilidad de anchos" if self.tile_mode else "Variabilidad de alturas"
        stats = self.belt.stats
        if stats.count:
            process = self.height_sensor.process
        else:
            try:
                process = self.configure_height_sensor(
                    {key: variable.get().strip() for key, variable in self.fields.items()}
                ).process
            except ValueError as error:
                messagebox.showerror(title, str(error), parent=self.parent)
                return
        lower, upper = process.limits
        detail = f"Método: {process.method}\nLímites: {lower:g}–{upper:g} mm"
        if process.sigma:
            detail += f"\nMedia configurada: {process.mean:g} mm\nSigma configurada: {process.sigma:.4g} mm"
        if not stats.count:
            if process.method == "Buenas por cada 100":
                detail += f"\n{process.good_per_batch} piezas dentro de rango por cada lote de 100."
            detail += "\n\nInicia la cinta para calcular las estadísticas de las piezas generadas."
            messagebox.showinfo(title, detail, parent=self.parent)
            return
        detail += (f"\n\nPiezas: {stats.count} · Media muestral: {stats.mean:.3f} mm\n"
                   f"Dentro de rango: {stats.good}/{stats.count} ({stats.good / stats.count:.1%})\n"
                   f"{'Estrechas' if self.tile_mode else 'Bajas'}: {stats.low} · "
                   f"{'Anchas' if self.tile_mode else 'Altas'}: {stats.count - stats.good - stats.low}")
        variance = stats.variance
        if variance is not None:
            sigma = sqrt(variance)
            detail += f"\nDesviación típica muestral (sigma): {sigma:.3f} mm\nVarianza: {variance:.3f} mm²"
            if sigma > 0 and process.method in ("Normal (sigma)", "Normal (nivel sigma)"):
                cp = (upper - lower) / (6 * sigma)
                cpk = min(upper - stats.mean, stats.mean - lower) / (3 * sigma)
                detail += f"\nCp: {cp:.2f} · Cpk: {cpk:.2f}"
        messagebox.showinfo(title, detail, parent=self.parent)

    def show_mode(self):
        if not self.height_mode and self.fields["modo"].get() == "Variador %MW":
            self.bit_panel.grid_remove()
            self.drive_panel.grid()
        else:
            self.drive_panel.grid_remove()
            self.bit_panel.grid()

    def reset_session(self):
        self.direction = 0
        self.height_sensor = None
        self.selector.set("Elegir…")
        self.draw_selector()
        self.meters = (None,) * len(METERS)
        self.update_signals((False,) * len(SIGNALS))
        self.status.set(self.initial_status())
        self.button_status.set("Mantén pulsado para enviar el estado elegido.")
        self.reset()

    @staticmethod
    def bit(raw, label):
        if not raw:
            return None
        try:
            address = parse_address(raw)
        except ValueError as error:
            raise ValueError(f"{configured(label, raw)}. Dirección de bit inválida. {error}") from None
        if not address.coil and address.bit is None:
            raise ValueError(f"{configured(label, raw)}. Se necesita un bit de memoria.")
        return address

    @staticmethod
    def word(raw, label):
        if not raw:
            return None
        original = raw
        if raw.isascii() and raw.isdecimal():
            raw = f"%MW{raw}"
        try:
            address = parse_address(raw)
        except ValueError as error:
            raise ValueError(f"{configured(label, original)}. Dirección de palabra inválida. {error}") from None
        if address.coil or address.bit is not None:
            raise ValueError(f"{configured(label, original)}. Se necesita una palabra %MW completa.")
        return address

    @staticmethod
    def display_address(raw, label):
        if not raw:
            return None
        try:
            return parse_address(f"%MW{raw}" if raw.isascii() and raw.isdecimal() else raw)
        except ValueError as error:
            raise ValueError(f"{configured(label, raw)}. {error}") from None

    def configure_height_sensor(self, fields):
        name = "Sensores de ancho" if self.tile_mode else "Sensor difuso"
        keys = ("altura_baja", "altura_nominal", "altura_alta", "tolerancia", "altura_10v")
        descriptions = (("Ancho pequeño", "Ancho nominal", "Ancho grande", "Tolerancia", "Ancho a 10 V")
                        if self.tile_mode else ("Pieza baja", "Altura nominal", "Pieza alta", "Tolerancia", "Altura a 10 V"))
        low, nominal, high, tolerance, full_height = (
            nonnegative_number(fields[key], f"{name} / {description} (mm)")
            for key, description in zip(keys, descriptions)
        )
        if not 0 < low < nominal < high:
            quantity = "anchos positivos" if self.tile_mode else "alturas positivas"
            raise ValueError(f"{name}: valores = {low:g}, {nominal:g}, {high:g} mm. "
                             f"Usa tres {quantity} en orden ascendente.")
        if full_height == 0:
            raise ValueError(f"{name} / {descriptions[-1]} = 0 mm. Debe ser mayor que cero.")
        raw_word = fields["cuentas_10v"]
        try:
            full_word = int(raw_word)
        except ValueError:
            full_word = 0
        if not 1 <= full_word <= 65535:
            raise ValueError(f"{configured(f'{name} / Valor %MW a 10 V', raw_word)}. Usa un entero de 1 a 65535.")
        address = self.word(fields["salida_analogica"], f"{name} / Salida analógica")
        heights = (low, nominal, high)
        process = self.configure_process(fields, heights, tolerance)
        geometry = self.configure_tile_geometry(fields, high) if self.tile_mode else None
        return HeightSensor(address, heights, tolerance, full_height, full_word, process, geometry)

    def configure_tile_geometry(self, fields, maximum):
        shape = fields["forma_azulejo"]
        if shape not in (*TILE_SHAPES, MIXED_SHAPES):
            raise ValueError(f"{configured('Forma y posición / Forma', shape)}. Selecciona una forma disponible.")
        offsets = []
        for key, name in (("offset_min", "mínimo"), ("offset_max", "máximo")):
            raw = fields[key]
            try:
                offset = float(raw.replace(",", "."))
            except ValueError:
                offset = float("nan")
            if not isfinite(offset):
                raise ValueError(f"{configured(f'Forma y posición / Desplaz. {name}', raw)}. Introduce un número finito.")
            offsets.append(offset)
        if offsets[0] > offsets[1]:
            raise ValueError("Forma y posición: el desplazamiento mínimo supera al máximo.")
        full_scale = nonnegative_number(fields["distancia_10v"], "Forma y posición / Distancia a 10 V")
        if not full_scale:
            raise ValueError("Forma y posición / Distancia a 10 V = 0. Debe ser mayor que cero.")
        addresses = tuple(self.word(fields[key], f"Forma y posición / {label}")
                          for key, label in self.tile_channels())
        reference_half = maximum * .60 + max(map(abs, offsets)) + 5
        return TileGeometry(shape, tuple(offsets), full_scale, addresses, reference_half, self.corner_mode)

    @staticmethod
    def configure_process(fields, heights, tolerance):
        minimum, nominal, maximum = heights
        method = fields["metodo_altura"]
        if method not in PROCESS_METHODS:
            raise ValueError(f"{configured('Proceso / Método', method)}. Selecciona un método del desplegable.")
        mean, sigma, good = nominal, 0.0, 100
        if method in ("Normal (sigma)", "Normal (nivel sigma)"):
            raw_mean = fields["media_altura"]
            mean = nonnegative_number(raw_mean, "Proceso / Media (mm)") if raw_mean else nominal
            if not minimum <= mean <= maximum:
                raise ValueError(f"Proceso / Media = {mean:g} mm. Debe estar entre {minimum:g} y {maximum:g} mm.")
            if method == "Normal (sigma)":
                sigma = nonnegative_number(fields["sigma_altura"], "Proceso / Sigma (mm)")
            else:
                level = nonnegative_number(fields["nivel_sigma"], "Proceso / Nivel sigma")
                distance = tolerance - abs(mean - nominal)
                if level == 0 or distance <= 0:
                    raise ValueError(f"Proceso / Nivel sigma = {level:g}, media = {mean:g} mm. "
                                     "Usa un nivel positivo y una media dentro de la tolerancia.")
                sigma = distance / level
            if sigma <= 0 or not sigma < float("inf"):
                raise ValueError("Proceso / Sigma. La desviación típica debe ser positiva y finita.")
        elif method == "Buenas por cada 100":
            raw_good = fields["buenas_100"]
            try:
                good = int(raw_good)
            except ValueError:
                good = -1
            if not 0 <= good <= 100:
                raise ValueError(f"{configured('Proceso / Buenas de cada 100', raw_good)}. Usa un entero de 0 a 100.")
            if good < 100 and not (minimum < nominal - tolerance and maximum > nominal + tolerance):
                raise ValueError("Proceso / Buenas de cada 100. Para generar piezas fuera de rango, "
                                 "ajusta la altura baja y la alta por fuera de ambos límites de tolerancia.")
        return HeightProcess(method, heights, tolerance, mean, sigma, good)

    def configuration(self, fields):
        right = left = drive = None
        minimum, maximum = -100, 100
        movement = "Movimiento y entrada de cajas"
        drive_label = f"{movement} / Consigna del variador"
        if not self.height_mode and fields["modo"] == "Variador %MW":
            raw = fields["variador"]
            drive = self.word(raw, drive_label)
            if drive is None:
                raise ValueError(f"{configured(drive_label, raw)}. Introduce una palabra %MW.")
            limits = (f"{movement} / Variador {raw}: {configured('Izquierda máx.', fields['minimo'])}, "
                      f"{configured('Derecha máx.', fields['maximo'])}")
            try:
                minimum, maximum = int(fields["minimo"]), int(fields["maximo"])
            except ValueError:
                raise ValueError(f"{limits}. Introduce dos enteros.") from None
            if not -32768 <= minimum < 0 < maximum <= 32767:
                raise ValueError(f"{limits}. El mínimo debe estar entre -32768 y -1 y el máximo entre 1 y 32767.")
        else:
            right = self.bit(fields["derecha"], f"{movement} / Marcha {'adelante' if self.height_mode else 'derecha'}")
            left = self.bit(fields["izquierda"], f"{movement} / Marcha {'atrás' if self.height_mode else 'izquierda'}")
        sensor_fields = []
        for index, (name, _, size) in enumerate(SENSORS):
            label, raw = f"Sensores / {name} / {size}", fields.get(f"sensor{index}", "")
            sensor_fields.append(AddressField(label, raw, self.bit(raw, label)))
        signal_fields = []
        for key, name, _ in SIGNALS:
            label, raw = f"Avisos / {name}", fields[key]
            signal_fields.append(AddressField(label, raw, self.bit(raw, label)))
        meters = tuple(self.display_address(fields[key], f"Displays / {fields[f'{key}_nombre'] or name}")
                       for key, name in METERS)
        meter_names = tuple(fields[f"{key}_nombre"] or name for key, name in METERS)
        commands = [AddressField(f"{movement} / Marcha derecha", fields["derecha"], right),
                    AddressField(f"{movement} / Marcha izquierda", fields["izquierda"], left)]
        drive_field = [AddressField(drive_label, fields["variador"], drive)]
        check_addresses(commands)
        check_addresses(commands, sensor_fields)
        check_addresses(sensor_fields, drive_field, shared_word=True)
        buttons = []
        for key, label, _ in BUTTONS:
            symbol = fields[f"{key}_simbolo"]
            title = f"Pulsadores / {label}" + (f" ({symbol})" if symbol else "")
            raw = fields[f"{key}_bit"]
            buttons.append(AddressField(title, raw, self.bit(raw, title)))
        selector = []
        for index in (1, 2):
            symbol = fields[f"selector_simbolo{index}"]
            label = f"Pulsadores / Selector / Posición {index}" + (f" ({symbol})" if symbol else "")
            raw = fields[f"selector_bit{index}"]
            selector.append(AddressField(label, raw, self.bit(raw, label)))
        check_addresses(selector)
        check_addresses([*buttons, *selector], sensor_fields)
        check_addresses([*buttons, *selector], drive_field, shared_word=True)
        height_sensor = self.configure_height_sensor(fields) if self.height_mode else None
        if height_sensor is not None:
            output = AddressField("Medición / Salida analógica", fields["salida_analogica"], height_sensor.address)
            measurements = [output]
            if height_sensor.tile_geometry is not None:
                measurements.extend(AddressField(f"Forma y posición / {label}", fields[key], address)
                                    for (key, label), address in zip(
                                        self.tile_channels(), height_sensor.tile_geometry.sensor_addresses))
            check_addresses(measurements)
            check_addresses(measurements, [*commands, *buttons, *selector], shared_word=True)
        speed = nonnegative_number(fields["velocidad"], f"{movement} / Velocidad % de la cinta")
        interval = nonnegative_number(fields["intervalo"], f"{movement} / Tiempo entre cajas")
        if not 0 < speed <= 60:
            raise ValueError(f"{movement} / Velocidad % de la cinta = {speed:g}. Usa un valor mayor que 0 y hasta 60 % por segundo.")
        if interval < 0.25:
            raise ValueError(f"{movement} / Tiempo entre cajas = {interval:g} s. El mínimo es 0,25 s.")
        limit = None
        if fields["alimentacion"] == "Cantidad fija":
            try:
                limit = int(fields["cantidad"])
            except ValueError:
                raise ValueError(f"{configured(f'{movement} / Cantidad fija', fields['cantidad'])}. Introduce un entero.") from None
            if limit < 1:
                raise ValueError(f"{movement} / Cantidad fija = {limit}. Debe ser de al menos una caja.")
        sensors = tuple(field.address for field in sensor_fields)
        signals = tuple(field.address for field in signal_fields)
        sizes = METAL_SIZES if self.height_mode else SIZES
        size = fields["tamano"]
        if size not in (*sizes, "Aleatorias"):
            raise ValueError(f"{configured(f'{movement} / Tipo de pieza', size)}. Selecciona un tipo disponible.")
        return BeltConfig(right, left, drive, minimum, maximum, sensors, signals, meters, meter_names,
                          speed / 100, interval, size, limit, height_sensor)

    def toggle(self):
        if self.cancel is not None:
            self.pause()
            return
        fields = {key: variable.get().strip() for key, variable in self.fields.items()}
        try:
            config = self.configuration(fields)
            analog = config.height_sensor.address if config.height_sensor is not None else None
            geometry = config.height_sensor.tile_geometry if config.height_sensor is not None else None
            sensor_words = geometry.sensor_addresses if geometry is not None else ()
            endpoint = self.endpoint() if any((config.right, config.left, config.drive, analog,
                                               *sensor_words, *config.sensors, *config.signals, *config.meters)) else None
        except ValueError as error:
            startup_error(self.parent, "la cinta", self.status, error)
            return
        previous, self.saved = self.saved, fields
        if not self.save():
            self.saved = previous
            self.status.set("No se pudo guardar la configuración de la cinta.")
            return
        if (self.height_sensor is not None and config.height_sensor is not None
                and (self.height_sensor.process != config.height_sensor.process
                     or self.height_sensor.tile_geometry != config.height_sensor.tile_geometry)):
            self.belt = Belt()
        self.config = config
        self.height_sensor = config.height_sensor
        self.target = endpoint
        self.last_sample = None
        self.direction = 0
        self.speed_ratio = 0.0
        self.meters = (None,) * len(METERS)
        self.published = {}
        self.cancel = threading.Event()
        self.start_button.configure(text="Pausar")
        self.reset_button.configure(state="disabled")
        self.mode_picker.configure(state="disabled")
        for choice in self.choices:
            choice.configure(state="disabled")
        for editor in self.editors:
            editor.configure(state="disabled")
        self.status.set("Iniciando cinta…")
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
        self.reset_button.configure(state="normal")
        self.mode_picker.configure(state="readonly")
        for choice in self.choices:
            choice.configure(state="readonly")
        for editor in self.editors:
            editor.configure(state="normal")
        self.status.set("Cinta en pausa.")
        self.update_signals((False,) * len(SIGNALS))
        self.draw()

    def reset(self):
        self.belt = Belt()
        self.draw()

    def schedule(self, delay=SAMPLE_MS):
        if self.cancel is not None and not self.in_flight and self.timer is None:
            self.timer = self.parent.after(delay, self.sample)

    def sample(self):
        self.timer = None
        self.in_flight = True
        cancel = self.cancel
        args = (cancel, self.config, self.target, self.belt, self.last_sample,
                self.published.copy(), self.compatibility.get())
        threading.Thread(target=self.cycle, args=args, daemon=True).start()
        self.parent.after(15, self.poll, cancel)

    def cycle(self, cancel, config, endpoint, belt, last, published, compatibility):
        warning = ""
        errors = []
        drive_ok = True
        try:
            if config.drive is not None:
                word = self.exchange(*endpoint, config.drive, cancelled=cancel)
                if cancel.is_set():
                    self.results.put((None, ""))
                    return
                direction, ratio, value = config.drive_speed(word)
                warning = f"Consigna del variador: {value}."
            else:
                right, left = (
                    bool(self.exchange(*endpoint, address, cancelled=cancel)) if address is not None else False
                    for address in (config.right, config.left)
                )
                direction = int(right) - int(left)
                ratio = float(bool(direction))
                if right and left:
                    warning = "Ambos mandos de marcha están activos. Cinta parada."
        except (OSError, ProtocolError) as error:
            errors.append(f"No se pudo leer el mando de la cinta. {error}")
            direction, ratio, drive_ok = 0, 0.0, False
        if cancel.is_set():
            self.results.put((None, ""))
            return
        now = time.monotonic()
        # Keep each movement shorter than a box so it cannot skip a sensor.
        seconds = min(0.15, now - last) if last is not None else 0.0
        height_sensor = config.height_sensor
        sizes = METAL_SIZES if height_sensor is not None else SIZES
        process = height_sensor.process if height_sensor is not None else None
        if self.tile_mode and direction:
            min_width = MAX_TILE_LENGTH * process.heights[0] / process.heights[2]
            seconds = min(seconds, min_width / (config.speed * ratio))
        belt = belt.advance(direction, config.speed * ratio, config.interval, seconds,
                            config.size, config.limit, sizes, process,
                            tile_geometry=height_sensor.tile_geometry if height_sensor is not None else None)
        outputs = {}
        labels = {}
        for address, active, (name, _, size) in zip(config.sensors, belt.detect(), SENSORS):
            if address is not None:
                outputs[address] = max(outputs.get(address, 0), active)
                labels[address] = f"{name.lower()}, {size.lower()}"
        if height_sensor is not None and height_sensor.address is not None:
            address = height_sensor.address
            outputs[address] = height_sensor.measure(belt)[2]
            labels[address] = f"difuso / %MW{address.index}"
        if height_sensor is not None and height_sensor.tile_geometry is not None:
            geometry = height_sensor.tile_geometry
            if self.corner_mode:
                inspection = geometry.inspect_corners(belt)
                gaps = inspection.gaps if inspection is not None else None
            else:
                gaps = geometry.gaps(belt)
            for index, (address, (_, name)) in enumerate(zip(geometry.sensor_addresses, self.tile_channels())):
                if address is not None:
                    outputs[address] = geometry.sensor_word(gaps[index], height_sensor.full_word) if gaps else 0
                    labels[address] = f"{name} / %MW{address.index}"
        readbacks = {}
        for address, active in outputs.items():
            if cancel.is_set():
                break
            if published.get(address) != active:
                try:
                    self.exchange(*endpoint, address, active, compatibility, cancelled=cancel)
                    published[address] = active
                    readbacks[address] = active
                except (OSError, ProtocolError) as error:
                    errors.append(f"Sensor {labels[address]}: {error}")

        def read(address):
            if address is None:
                return None
            if address not in readbacks:
                readbacks[address] = self.exchange(*endpoint, address, cancelled=cancel)
            return readbacks[address]
        signals = []
        for address, (_, name, _) in zip(config.signals, SIGNALS):
            if cancel.is_set():
                self.results.put((None, ""))
                return
            try:
                signals.append(bool(read(address)))
            except (OSError, ProtocolError) as error:
                signals.append(False)
                errors.append(f"{name}: {error}")
        meters = []
        for address, name in zip(config.meters, config.meter_names):
            if cancel.is_set():
                self.results.put((None, ""))
                return
            try:
                meters.append(read(address))
            except (OSError, ProtocolError) as error:
                meters.append(None)
                errors.append(f"{name}: {error}")
        self.results.put(((belt, direction, ratio, now if drive_ok else None, published, tuple(signals), tuple(meters)),
                          " ".join(errors) or warning))

    def poll(self, cancel):
        try:
            result, error = self.results.get_nowait()
        except queue.Empty:
            self.parent.after(15, self.poll, cancel)
            return
        self.in_flight = False
        if self.cancel is cancel:
            if result is None:
                self.last_sample = None
                self.direction = 0
                self.speed_ratio = 0.0
                self.meters = (None,) * len(METERS)
                self.update_signals((False,) * len(SIGNALS))
                self.draw()
                self.motion.set("Cinta detenida por falta de señal")
            else:
                (self.belt, self.direction, self.speed_ratio, self.last_sample,
                 self.published, signals, self.meters) = result
                self.update_signals(signals)
                self.draw()
            self.status.set(error or ("Cinta en marcha." if self.direction else "Sin orden de marcha."))
        self.schedule(0 if self.cancel is not cancel else SAMPLE_MS)

    def update_signals(self, signals):
        self.signals = signals
        siren = signals[4]
        if siren != self.siren_playing and sys.platform == "win32":
            try:
                flags = winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_LOOP | winsound.SND_NODEFAULT
                winsound.PlaySound("SystemExclamation" if siren else None, flags if siren else 0)
            except RuntimeError:
                pass
        self.siren_playing = siren

    def draw(self):
        canvas = self.diagram
        canvas.delete("all")
        width = max(540, canvas.winfo_width())
        height = max(162, canvas.winfo_height())
        left, right = width * 0.09, width * 0.73
        if self.tile_mode:
            geometry = self.height_sensor.tile_geometry if self.height_sensor is not None else None
            cross_ratio = (2 * geometry.reference_half / self.height_sensor.heights[2]
                           if geometry is not None else 1.4)
            band = 0.39 if self.corner_mode else 0.46
            span = min(right - left, height * band / MAX_TILE_LENGTH / cross_ratio)
            center = (left + right) / 2
            left, right = center - span / 2, center + span / 2
        span = right - left
        rail_top, rail_bottom = height * 0.60, height * 0.76
        if self.tile_mode:
            rail_bottom = self.draw_top_belt(canvas, left, right, height)
        else:
            canvas.create_rectangle(left, rail_top, right, rail_bottom, fill="#334b5c", outline="")
        for index in (range(17) if not self.tile_mode else ()):
            x = left + span * (index + 0.5) / 17
            y = (rail_top + rail_bottom) / 2
            radius = min(8, (rail_bottom - rail_top) / 3)
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                               fill="#94a3b8", outline="#cbd5e1")

        for box in (() if self.tile_mode else self.belt.boxes):
            x = left + box.center * span
            half = box.half * span
            large = box.size == "Grandes"
            if self.height_sensor is not None:
                box_top = self.metal_top(box, rail_top)
                fill, outline = "#a2b4c3", "#455c6d"
            else:
                box_top = rail_top - min(51 if large else 31, height * (0.29 if large else 0.20))
                fill, outline = ("#d68849" if large else "#f2c68c"), "#844f2b"
            canvas.create_rectangle(x - half, box_top, x + half, rail_top,
                                    fill=fill, outline=outline, width=2, tags=("caja", box.size))
            if self.height_sensor is not None:
                canvas.create_line(x - half + 4, box_top + 4, x + half - 4, box_top + 4, fill="#dbe5eb")
            else:
                canvas.create_line(x, box_top, x, box_top + 9, fill=outline, width=2)

        if self.height_mode and not self.tile_mode:
            self.draw_height_sensor(canvas, (left + right) / 2, rail_top)
        stations = () if self.height_mode else zip(SENSORS, self.belt.detect())
        for index, ((name, position, size), active) in enumerate(stations):
            x = left + span * position
            color = "#167642" if active else "#64748b"
            small = size == "Pequeñas"
            y = rail_top - (14 if small else min(39, height * 0.24))
            if index < 3:
                canvas.create_text(x, 14, text=name, fill="#243746")
                canvas.create_line(x, 25, x, rail_top, fill="#8497a6", dash=(3, 3))
            canvas.create_oval(x - 6, y - 6, x + 6, y + 6, fill=color if active else "#f1f5f9",
                               outline=color, width=2, tags=("sensor", f"sensor-{index}"))
            canvas.create_text(x + 10, y, anchor="w", text="P" if small else "G", fill="#243746")
        bx = width * 0.83
        for index, ((key, name, color), active) in enumerate(zip(SIGNALS, self.signals)):
            y = 18 + index * 23
            self.draw_signal(canvas, key, bx, y, color, active)
            canvas.create_text(bx + 15, y, anchor="w", text=name, fill="#243746")
        fixed = self.fields["alimentacion"].get() == "Cantidad fija"
        goal = f" / {self.fields['cantidad'].get()}" if fixed else ""
        noun = "Azulejos" if self.tile_mode else "Piezas" if self.height_mode else "Cajas"
        self.counter.set(f"{noun} emitidas: {self.belt.emitted}{goal}")
        card_width = min(128, (right - left) / len(METERS) - 6)
        cards_x = (left + right - len(METERS) * (card_width + 6)) / 2
        bottom = height - 5
        for index, ((key, title), value) in enumerate(zip(METERS, self.meters)):
            x = cards_x + index * (card_width + 6)
            tags = ("medidor", key)
            canvas.create_rectangle(x, bottom - 29, x + card_width, bottom,
                                    fill="white", outline="#94a3b8", tags=tags)
            name = self.fields[f"{key}_nombre"].get().strip() or title
            short_name = name
            while short_name and self.meter_font.measure(short_name) > card_width - 8:
                short_name = short_name.rstrip("…")[:-1] + "…" if len(short_name.rstrip("…")) > 1 else ""
            canvas.create_text(x + card_width / 2, bottom - 21, text=short_name,
                               fill="#334b5c", font=self.meter_font, tags=tags)
            canvas.create_text(x + card_width / 2, bottom - 8,
                               text="—" if value is None else str(value),
                               fill="#243746", font=("Segoe UI", 9, "bold"), tags=tags)
        if self.cancel is None:
            self.motion.set("Cinta en pausa")
        elif not self.direction:
            self.motion.set("Cinta parada · 0 %")
        else:
            arrow_right = left + span * 0.28 if self.tile_mode else right
            start, end = (left, arrow_right) if self.direction > 0 else (arrow_right, left)
            arrow_y = min(rail_bottom + 10, bottom - 36)
            canvas.create_line(start, arrow_y, end, arrow_y,
                               arrow="last", width=3, fill="#1d668b", tags=("sentido",))
            side = "derecha" if self.direction > 0 else "izquierda"
            self.motion.set(f"Hacia la {side} · {self.speed_ratio:.0%} de la velocidad máxima")

    def draw_top_belt(self, canvas, left, right, height):
        span = right - left
        axis_y = height * 0.40
        sensor = self.height_sensor
        geometry = sensor.tile_geometry if sensor is not None else None
        scale = span * MAX_TILE_LENGTH / (sensor.heights[2] if sensor is not None else 60)
        reference = geometry.reference_half if geometry is not None else 41
        half_track = reference * scale
        top, bottom = axis_y - half_track, axis_y + half_track
        center = (left + right) / 2
        canvas.create_rectangle(left, top, right, bottom, fill="#344b5c", outline="#182e3d", width=2)
        canvas.create_line(left, axis_y, right, axis_y, fill="#758b9a", dash=(4, 6))
        piece = None
        for box in self.belt.boxes:
            x = left + box.center * span
            half = box.half * span
            upper = [(x + (position * 2 - 1) * half, axis_y + (box.offset_mm + a * box.height_mm) * scale)
                     for position, a, b in box.profile]
            lower = [(x + (position * 2 - 1) * half, axis_y + (box.offset_mm + b * box.height_mm) * scale)
                     for position, a, b in reversed(box.profile)]
            if upper and lower:
                points = [coordinate for point in upper + lower for coordinate in point]
                canvas.create_polygon(*points, fill="#d0dcdf", outline="#40596c", width=2, tags=("azulejo",))
            if abs(box.center - 0.5) <= box.half:
                piece = box
        if self.corner_mode:
            self.draw_corner_inspection(canvas, left, right, top, bottom, axis_y, scale, piece)
            return bottom
        gaps = geometry.gaps(self.belt) if geometry is not None else None
        edges = piece.edges(.5) if piece is not None else (0, 0)
        beam_color = "#fa745e" if piece is not None else "#ac8179"
        for index, (direction, edge) in enumerate(((-1, top), (1, bottom)), 1):
            outer = edge + 10 * direction
            terminal = axis_y + edges[index - 1] * scale
            canvas.create_rectangle(center - 11, min(edge, outer), center + 11, max(edge, outer),
                                    fill="#173347", outline="#0f2433", tags=("sensor_ancho", f"sensor_ancho_{index}"))
            canvas.create_line(center, edge, center, terminal, fill=beam_color, dash=(3, 2),
                               width=2, tags=("haz_ancho", f"haz_ancho_{index}"))
            if piece is not None:
                canvas.create_oval(center - 2, terminal - 2, center + 2, terminal + 2,
                                   fill=beam_color, outline="")
            label = "Superior" if index == 1 else "Inferior"
            reading = "— mm · 0.00 V · 0"
            if gaps is not None:
                gap = gaps[index - 1]
                voltage = 10 * min(1.0, gap / geometry.gap_full_scale)
                word = geometry.sensor_word(gap, sensor.full_word)
                reading = f"{gap:.2f} mm · {voltage:.2f} V · {word}"
            canvas.create_text(center + 19, edge + direction * 10, text=f"{label}: {reading}",
                               anchor="sw" if direction < 0 else "nw",
                               fill="#243746", tags=("lectura_sensor_ancho",))
        measured, voltage, word, classification = (
            self.height_sensor.measure(self.belt) if self.height_sensor is not None else (0, 0, 0, "Sin pieza")
        )
        label = {"Más alta": "Más ancho", "Más baja": "Más estrecho",
                 "Correcta": "Correcto", "Sin pieza": "Sin pieza"}[classification]
        width_text = f"{measured:g} mm" if piece is not None else "— mm"
        canvas.create_text(left, max(10, top - 14), text=f"{label} · {width_text} · {voltage:.2f} V · {word}",
                           anchor="w", fill="#243746", tags=("medicion_ancho",))
        return bottom

    def draw_corner_inspection(self, canvas, left, right, top, bottom, axis_y, scale, piece):
        sensor = self.height_sensor
        geometry = sensor.tile_geometry if sensor is not None else None
        inspection = geometry.inspect_corners(self.belt) if geometry is not None else None
        center = (left + right) / 2
        width = right - left
        targets = []
        if inspection is not None and piece is not None:
            tile_start = left + (piece.center - piece.half) * width
            targets = [(tile_start + x * scale, axis_y + y * scale) for x, y in inspection.points]
        else:
            targets = [(center + offset, axis_y) for offset in (-32, 32, 32, -32)]
        readings = []
        for index, (x, y) in enumerate(targets):
            upper = index < 2
            edge, direction = (top, -1) if upper else (bottom, 1)
            casing = edge + 10 * direction
            tags = ("sensor_esquina", f"esquina-{index + 1}")
            canvas.create_rectangle(x - 8, min(edge, casing), x + 8, max(edge, casing),
                                    fill="#173347", outline="#0f2433", tags=tags)
            canvas.create_line(x, edge, x, y, fill="#fa745e" if inspection else "#9b6d69",
                               dash=(3, 2), width=2, tags=("haz_esquina",))
            if inspection is None:
                readings.append(f"S{index + 1} —")
            else:
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#fa745e", outline="")
                gap = inspection.gaps[index]
                volts = min(10.0, 10 * gap / geometry.gap_full_scale)
                word = geometry.sensor_word(gap, sensor.full_word)
                readings.append(f"S{index + 1}  {gap:.1f} mm / {volts:.2f} V / {word}")
        canvas.create_text(left, top - 13, anchor="sw", text="    ".join(readings[:2]),
                           fill="#243746", tags=("lectura_esquina",))
        canvas.create_text(left + width * .31, bottom + 13, anchor="nw", text="    ".join(readings[2:]),
                           fill="#243746", tags=("lectura_esquina",))
        if inspection is None:
            self.corner_summary.set("Sin pieza en la estación de inspección.")
        else:
            width_a, width_b = inspection.widths
            length_a, length_b = inspection.lengths
            measured, voltage, word, _ = sensor.measure(self.belt)
            self.corner_summary.set(
                f"Anchos {width_a:.2f} / {width_b:.2f} mm · Largos {length_a:.2f} / {length_b:.2f} mm · "
                f"Desplazamiento {inspection.offset:+.2f} mm · Δ diagonales {inspection.diagonal_difference:.2f} mm\n"
                f"Error máx. respecto a 90°: {inspection.angle_error:.2f}° · "
                f"Ancho medio {measured:.2f} mm / {voltage:.2f} V / {word}"
            )

    def metal_top(self, box, rail_top):
        return rail_top - min(rail_top - 45, 75) * self.height_sensor.height(box) / self.height_sensor.heights[2]

    def draw_height_sensor(self, canvas, x, rail_top):
        canvas.create_line(x - 52, 24, x - 12, 24, fill="#718498", width=3)
        canvas.create_rectangle(x - 13, 13, x + 13, 34, fill="#30495d", outline="#172938",
                                width=2, tags=("sensor_difuso",))
        canvas.create_oval(x - 4, 28, x + 4, 34, fill="#fb6758", outline="")
        canvas.create_text(x + 21, 22, text="Sensor difuso", anchor="w", fill="#243746")
        piece = next((box for box in self.belt.boxes if abs(box.center - 0.5) <= box.half), None)
        hit = self.height_sensor is not None and piece is not None
        end = self.metal_top(piece, rail_top) if hit else rail_top
        color = "#e35041" if hit else "#9b6d69"
        canvas.create_line(x, 34, x, end, fill=color, dash=(4, 3), width=2, tags=("haz",))
        if hit:
            canvas.create_oval(x - 3, end - 3, x + 3, end + 3, fill=color, outline="")
        height, voltage, word, state = (self.height_sensor.measure(self.belt) if self.height_sensor is not None
                                        else (0.0, 0.0, 0, "Sin pieza"))
        measured = f"{height:g} mm" if hit else "— mm"
        canvas.create_text(x + 16, 46, text=f"{state} · {measured} · {voltage:.2f} V · {word}",
                           anchor="w", fill="#243746", tags=("medicion",))

    @staticmethod
    def draw_signal(canvas, key, x, y, color, active):
        tags = ("aviso", key)
        fill = color if active else "#dce5ed"
        if key == "sirena":
            canvas.create_polygon(x - 9, y - 3, x - 4, y - 3, x + 3, y - 8,
                                  x + 3, y + 8, x - 4, y + 3, x - 9, y + 3,
                                  fill=fill, outline=color, tags=tags)
            canvas.create_arc(x - 2, y - 8, x + 10, y + 8, start=-64, extent=128,
                              style="arc", outline=color, width=2, tags=tags)
        elif key == "luz_emergencia":
            canvas.create_arc(x - 7, y - 8, x + 7, y + 6, start=0, extent=180,
                              fill=fill, outline=color, style="chord", tags=tags)
            canvas.create_rectangle(x - 8, y - 1, x + 8, y + 5, fill=fill, outline=color, tags=tags)
            for offset in (-8, 0, 8):
                canvas.create_line(x + offset, y - 9, x + offset * 1.3, y - 13,
                                   fill=color, tags=tags)
        else:
            canvas.create_oval(x - 8, y - 8, x + 8, y + 8, outline=color, width=2,
                               fill=fill, tags=tags)
