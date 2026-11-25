"""Dos depósitos de origen alimentan un depósito de mezcla."""

import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from modbus import Address, ProtocolError, parse_address
from simulation import (SAMPLE_MS, AddressField, Sensor, Valve, check_addresses, configured,
                        draw_vessel, nonnegative_number, parse_valve, startup_error)


@dataclass(frozen=True)
class MixingConfig:
    capacities: tuple
    initial: tuple
    valves: tuple[Valve, Valve, Valve]
    volume_outputs: tuple[Address | None, ...] = (None, None, None)
    sensors: tuple[tuple[Sensor | None, ...], ...] = ((), (), ())

    def transfer(self, volumes, flows, elapsed):
        minutes = elapsed / 60
        if minutes <= 0:
            return volumes, (0.0, 0.0, 0.0)
        first, second, target = volumes
        requested = (min(first, flows[0] * minutes), min(second, flows[1] * minutes))
        total = sum(requested)
        discharged = min(target + total, flows[2] * minutes)
        free = self.capacities[2] - target + discharged
        if total > free:
            requested = tuple(amount * (free / total) for amount in requested)
        moved_first, moved_second = requested
        updated = (first - moved_first, second - moved_second,
                   max(0.0, min(self.capacities[2], target + sum(requested) - discharged)))
        return updated, tuple(amount / minutes for amount in (*requested, discharged))


class MixerSimulator:
    def __init__(self, parent, endpoint, exchange, save, compatibility):
        self.parent = parent
        self.endpoint = endpoint
        self.exchange = exchange
        self.save = save
        self.compatibility = compatibility
        self.saved = {}
        self.fields = {}
        self.editors = []
        self.names = []
        self.levels = []
        self.rates = []
        self.results = queue.Queue()
        self.cancel = None
        self.in_flight = False
        self.timer = None
        self.config = None
        self.volumes = (100.0, 100.0, 0.0)
        self.contributions = (0.0, 0.0)
        self.initial_remaining = 0.0
        self.openings = None
        self.flows = (0.0, 0.0, 0.0)
        self.sample_time = None
        self.published = {}
        self.status = tk.StringVar(value="Los depósitos 1 y 2 descargan en el depósito 3.")

        self.viewport = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.viewport.yview)
        self.viewport.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.viewport.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(self.viewport)
        window = self.viewport.create_window(0, 0, window=content, anchor="nw")
        self.viewport.bind("<Configure>", lambda event: self.viewport.itemconfigure(window, width=event.width))
        content.bind("<Configure>", lambda event: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        parent.winfo_toplevel().bind("<MouseWheel>", self.scroll, add="+")

        actions = ttk.Frame(content)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="Iniciar", command=self.toggle)
        self.start_button.pack(side="left")
        self.reset_button = ttk.Button(actions, text="Restablecer volúmenes", command=self.reset)
        self.reset_button.pack(side="left", padx=8)
        self.total = tk.StringVar()
        ttk.Label(actions, textvariable=self.total, font=("Segoe UI", 11, "bold")).pack(side="left", padx=8)
        ttk.Label(content, textvariable=self.status, wraplength=960).pack(anchor="w", pady=(0, 6))

        tanks = ttk.Frame(content)
        tanks.pack(fill="x")
        for index, color in enumerate(("#38a1d1", "#237d70", "#7358a6")):
            tanks.columnconfigure(index, weight=1, uniform="tank")
            title = f"Origen {index + 1} → Mezcla" if index < 2 else "Depósito de mezcla"
            card = ttk.LabelFrame(tanks, text=title, padding=8)
            card.grid(row=0, column=index, sticky="nsew", padx=4)
            self.field(card, index, "nombre", "Nombre", 0, f"Depósito {index + 1}" if index < 2 else "Mezcla")
            self.field(card, index, "capacidad", "Capacidad (L)", 1, "100" if index < 2 else "200")
            self.field(card, index, "inicio", "Volumen inicial (L)", 2, "100" if index < 2 else "0")
            self.field(card, index, "volumen_mw", "Volumen actual → %MW", 3, "")
            self.field(card, index, "direccion", "Mando descarga %MW", 4, "")
            self.field(card, index, "min", "Mínimo / máximo", 5, "0", width=7)
            self.field(card, index, "max", "", 5, "100", column=2, width=7)
            self.field(card, index, "caudal", "Caudal máximo (L/min)", 6, "20" if index < 2 else "0")
            ttk.Label(card, text="Sensor").grid(row=7, column=0, sticky="w")
            ttk.Label(card, text="Umbral (L)").grid(row=7, column=1, sticky="w")
            ttk.Label(card, text="Bit de memoria").grid(row=7, column=2, sticky="w")
            for sensor_index in (1, 2):
                row = 7 + sensor_index
                self.field(card, index, f"s{sensor_index}_nivel", f"Sensor {sensor_index}", row, "", width=8)
                self.field(card, index, f"s{sensor_index}_bit", "", row, "", column=2, width=11)
            name = tk.StringVar()
            level = tk.StringVar()
            rate = tk.StringVar()
            self.names.append(name)
            self.levels.append(level)
            self.rates.append(rate)
            ttk.Label(card, textvariable=name, font=("Segoe UI", 11, "bold")).grid(
                row=10, column=0, columnspan=3, sticky="w", pady=(7, 0),
            )
            ttk.Label(card, textvariable=level).grid(row=11, column=0, columnspan=3, sticky="w")
            ttk.Label(card, textvariable=rate, foreground=color, wraplength=270).grid(
                row=12, column=0, columnspan=3, sticky="w",
            )
        self.diagram = tk.Canvas(content, background="#f1f5f9", highlightthickness=0, height=300)
        self.diagram.pack(fill="both", expand=True, pady=(8, 0))
        self.diagram.bind("<Configure>", lambda event: self.draw())
        for variable in self.fields.values():
            variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    def scroll(self, event):
        widget = self.parent.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is self.viewport:
                self.viewport.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            widget = widget.master

    def field(self, card, index, key, title, row, default, column=1, width=13):
        variable = tk.StringVar(value=default)
        self.fields[f"{index + 1}_{key}"] = variable
        if title:
            ttk.Label(card, text=title).grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)
        editor = ttk.Entry(card, textvariable=variable, width=width)
        editor.grid(row=row, column=column, sticky="w", padx=(0, 4), pady=2)
        self.editors.append(editor)

    def load(self, settings):
        if isinstance(settings, dict):
            self.saved = {key: settings[key] for key in self.fields if isinstance(settings.get(key), str)}
            for key, value in self.saved.items():
                self.fields[key].set(value)

    def reset_session(self):
        self.config = None
        self.contributions = (0.0, 0.0)
        self.initial_remaining = 0.0
        self.openings = None
        self.flows = (0.0, 0.0, 0.0)
        self.status.set("Los depósitos 1 y 2 descargan en el depósito 3.")
        self.draw()

    def configuration(self, fields):
        capacities, initial, valves, outputs, sensors = [], [], [], [], []
        volume_fields, valve_fields, sensor_fields = [], [], []
        for index in (1, 2, 3):
            name = f"Depósito {index} ({fields[f'{index}_nombre'] or 'sin nombre'})"
            capacity = nonnegative_number(fields[f"{index}_capacidad"], f"{name} / Capacidad (L)")
            volume = nonnegative_number(fields[f"{index}_inicio"], f"{name} / Volumen inicial (L)")
            if capacity == 0 or volume > capacity:
                raise ValueError(f"{name}: capacidad = {capacity:g} L, volumen inicial = {volume:g} L. "
                                 "La capacidad debe ser positiva y contener el volumen inicial.")
            capacities.append(capacity)
            initial.append(volume)
            raw = fields.get(f"{index}_volumen_mw", "")
            original = raw
            if raw.isascii() and raw.isdecimal():
                raw = f"%MW{raw}"
            try:
                output = parse_address(raw) if raw else None
            except ValueError as error:
                raise ValueError(f"{configured(f'{name} / Volumen actual', original)}. {error}") from None
            if output is not None:
                if output.coil or output.bit is not None:
                    raise ValueError(f"{configured(f'{name} / Volumen actual', original)}. Se requiere una palabra %MW completa.")
                if capacity > 65535:
                    raise ValueError(f"{name} / Capacidad = {capacity:g} L. La palabra {original} admite hasta 65535 L.")
            outputs.append(output)
            volume_fields.append(AddressField(f"{name} / Volumen actual", original, output))
            valves.append(parse_valve(
                fields[f"{index}_direccion"], fields[f"{index}_min"], fields[f"{index}_max"],
                fields[f"{index}_caudal"], f"{name} / Mando descarga",
            ))
            valve_fields.append(AddressField(f"{name} / Mando descarga", fields[f"{index}_direccion"], valves[-1].address))
            group = tuple(self.parse_sensor(fields, index, number, capacity, name) for number in (1, 2))
            sensors.append(group)
            sensor_fields.extend(AddressField(f"{name} / Sensor {number} / Bit", fields[f"{index}_s{number}_bit"],
                                              sensor.address)
                                 for number, sensor in enumerate(group, 1) if sensor is not None)
        check_addresses(volume_fields)
        check_addresses(volume_fields, valve_fields)
        check_addresses(sensor_fields)
        check_addresses(sensor_fields, [*volume_fields, *valve_fields], shared_word=True)
        return MixingConfig(tuple(capacities), tuple(initial), tuple(valves), tuple(outputs), tuple(sensors))

    @staticmethod
    def parse_sensor(fields, index, number, capacity, name):
        prefix = f"{index}_s{number}"
        raw_level = fields[f"{prefix}_nivel"]
        raw_bit = fields[f"{prefix}_bit"]
        label = f"{name} / Sensor {number}"
        if not raw_level and not raw_bit:
            return None
        level = nonnegative_number(raw_level, f"{label} / Umbral (L), bit {raw_bit or '(vacío)'}")
        if level > capacity:
            raise ValueError(f"{label}: umbral = {level:g} L, capacidad = {capacity:g} L. {configured('Bit', raw_bit)}.")
        try:
            address = parse_address(raw_bit) if raw_bit else None
        except ValueError as error:
            raise ValueError(f"{configured(f'{label} / Bit', raw_bit)}. {error}") from None
        if address is not None and not address.coil and address.bit is None:
            raise ValueError(f"{configured(f'{label} / Bit', raw_bit)}. Se necesita un bit de memoria.")
        return Sensor(level, address)

    def toggle(self):
        if self.cancel is not None:
            self.pause()
            return
        fields = {key: variable.get().strip() for key, variable in self.fields.items()}
        try:
            config = self.configuration(fields)
            uses_modbus = any(config.volume_outputs) or any(valve.address for valve in config.valves)
            uses_modbus = uses_modbus or any(sensor.address for group in config.sensors
                                             for sensor in group if sensor is not None)
            self.target = self.endpoint() if uses_modbus else None
        except ValueError as error:
            startup_error(self.parent, "la mezcla", self.status, error)
            return
        previous = self.saved
        self.saved = fields
        if not self.save():
            self.saved = previous
            self.status.set("No se pudo guardar la configuración de la mezcla.")
            return
        if self.config is None or (config.capacities, config.initial) != (self.config.capacities, self.config.initial):
            self.set_initial_volumes(config)
        self.config = config
        self.sample_time = None
        self.published = {}
        self.openings = None
        self.flows = (0.0, 0.0, 0.0)
        self.cancel = threading.Event()
        self.start_button.configure(text="Pausar")
        self.reset_button.configure(state="disabled")
        for editor in self.editors:
            editor.configure(state="disabled")
        self.status.set("Mezcla en marcha.")
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
        for editor in self.editors:
            editor.configure(state="normal")
        self.flows = (0.0, 0.0, 0.0)
        self.status.set("Mezcla en pausa.")
        self.draw()

    def reset(self):
        try:
            config = self.configuration({key: variable.get().strip() for key, variable in self.fields.items()})
        except ValueError as error:
            self.status.set(str(error))
            return
        self.config = config
        self.set_initial_volumes(config)
        self.openings = None
        self.flows = (0.0, 0.0, 0.0)
        self.status.set("Volúmenes iniciales restablecidos.")
        self.draw()

    def set_initial_volumes(self, config):
        self.volumes = config.initial
        self.contributions = (0.0, 0.0)
        self.initial_remaining = config.initial[2]

    def schedule(self, delay=SAMPLE_MS):
        if self.cancel is not None and not self.in_flight and self.timer is None:
            self.timer = self.parent.after(delay, self.sample)

    def sample(self):
        self.timer = None
        self.in_flight = True
        cancel = self.cancel
        threading.Thread(target=self.read_openings, args=(cancel, self.target, self.config.valves),
                         daemon=True).start()
        self.parent.after(15, self.poll, cancel)

    def read_openings(self, cancel, endpoint, valves):
        readings = []
        for index, valve in enumerate(valves, start=1):
            if cancel.is_set():
                self.results.put((None, None))
                return
            try:
                if valve.address is None:
                    readings.append(1.0)
                else:
                    word = self.exchange(*endpoint, valve.address, cancelled=cancel)
                    if cancel.is_set():
                        self.results.put((None, None))
                        return
                    readings.append(valve.opening(word))
            except (OSError, ProtocolError) as error:
                self.results.put((None, f"Descarga del depósito {index}, %MW{valve.address.index}: {error}"))
                return
        self.results.put((tuple(readings), None))

    def pending_outputs(self):
        outputs = []
        for index, volume in enumerate(self.volumes):
            address = self.config.volume_outputs[index]
            if address is not None:
                outputs.append((address, int(volume + 0.5), f"Volumen del depósito {index + 1}"))
            for number, sensor in enumerate(self.config.sensors[index], start=1):
                if sensor is not None and sensor.address is not None:
                    outputs.append((sensor.address, int(volume >= sensor.threshold),
                                    f"Sensor {number} del depósito {index + 1}"))
        return [output for output in outputs if self.published.get(output[0]) != output[1]]

    def publish_outputs(self, cancel, endpoint, outputs, compatibility):
        published = {}
        failures = []
        for address, value, label in outputs:
            if cancel.is_set():
                break
            try:
                self.exchange(*endpoint, address, value, compatibility, cancelled=cancel)
                published[address] = value
            except (OSError, ProtocolError) as error:
                failures.append(f"{label}: {error}")
        self.results.put((published, " ".join(failures)))

    def poll(self, cancel, publishing=False):
        try:
            result, error = self.results.get_nowait()
        except queue.Empty:
            self.parent.after(15, self.poll, cancel, publishing)
            return
        self.in_flight = False
        if self.cancel is cancel:
            if publishing:
                self.published.update(result)
                self.status.set(error or "Mezcla en marcha.")
                self.schedule()
                return
            now = time.monotonic()
            if error:
                self.sample_time = None
                self.flows = (0.0, 0.0, 0.0)
                self.status.set(f"Mezcla en espera. {error}")
            else:
                if self.sample_time is not None:
                    requested = tuple(opening * valve.max_flow
                                      for opening, valve in zip(self.openings, self.config.valves))
                    before = self.volumes
                    self.volumes, self.flows = self.config.transfer(before, requested, now - self.sample_time)
                    added = tuple(before[index] - self.volumes[index] for index in range(2))
                    available = before[2] + sum(added)
                    retained = min(1.0, self.volumes[2] / available) if available else 0.0
                    self.contributions = tuple((volume + addition) * retained
                                               for volume, addition in zip(self.contributions, added))
                    self.initial_remaining *= retained
                self.sample_time = now
                self.status.set("Mezcla en marcha.")
            self.openings = result
            self.draw()
            outputs = self.pending_outputs() if not error else []
            if outputs:
                self.in_flight = True
                args = (cancel, self.target, outputs, self.compatibility.get())
                threading.Thread(target=self.publish_outputs, args=args, daemon=True).start()
                self.parent.after(15, self.poll, cancel, True)
                return
        self.schedule(0 if self.cancel is not cancel else SAMPLE_MS)

    def draw(self):
        incoming = sum(self.flows[:2])
        self.total.set(f"Entrada a la mezcla: {incoming:.2f} L/min    Descarga: {self.flows[2]:.2f} L/min")
        levels = []
        tank_values = []
        for index in range(3):
            prefix = str(index + 1)
            self.names[index].set(self.fields[f"{prefix}_nombre"].get().strip() or f"Depósito {prefix}")
            try:
                capacity = nonnegative_number(self.fields[f"{prefix}_capacidad"].get(), "La capacidad")
                initial = nonnegative_number(self.fields[f"{prefix}_inicio"].get(), "El volumen inicial")
            except ValueError:
                capacity, initial = 0.0, 0.0
            volume = self.volumes[index] if self.config else initial
            level = min(1.0, volume / capacity) if capacity else 0.0
            levels.append(level)
            tank_values.append((volume, capacity))
            self.levels[index].set(f"{volume:.2f} / {capacity:g} L  ·  {level:.1%}")
            if index < 2:
                opening = f"{self.openings[index]:.1%}" if self.openings is not None else "—"
                self.rates[index].set(f"Salida {self.flows[index]:.2f} L/min  ·  Apertura {opening}")
            else:
                opening = f"{self.openings[index]:.1%}" if self.openings is not None else "—"
                self.rates[index].set(f"Descarga {self.flows[index]:.2f} L/min  ·  Apertura {opening}")
        self.draw_diagram(levels, tank_values)

    def draw_diagram(self, levels, tank_values):
        canvas = self.diagram
        canvas.delete("all")
        width = max(440, canvas.winfo_width())
        height = max(225, canvas.winfo_height())
        middle = width / 2
        centers = (width * 0.24, width * 0.76, middle)
        half_width = min(104, width * 0.13)
        top = 28
        source_bottom = height * 0.40
        junction = height * 0.52
        target_top = height * 0.66
        inactive = "#8293a0"
        colors = ("#38a1d1", "#237d70", "#7358a6")

        for index, center in enumerate(centers[:2]):
            color = colors[index] if self.flows[index] > 0 else inactive
            canvas.create_line(center, source_bottom, center, junction, middle, junction,
                               fill=color, width=6, joinstyle="round", arrow="last",
                               arrowshape=(10, 13, 5), tags=("tuberia", f"origen-{index + 1}"))
            canvas.create_text((center + middle) / 2, junction - 13,
                               text=f"{self.flows[index]:.2f} L/min", fill="#334b5c")
        total = sum(self.flows[:2])
        canvas.create_line(middle, junction, middle, target_top, fill=colors[2] if total else inactive,
                           width=6, arrow="last", arrowshape=(10, 13, 5), tags=("tuberia", "mezcla"))
        canvas.create_text(middle + 14, (junction + target_top) / 2, anchor="w",
                           text=f"Total {total:.2f} L/min", fill="#334b5c")

        for index, (center, level, color) in enumerate(zip(centers, levels, colors)):
            vessel_top = top if index < 2 else target_top
            vessel_bottom = source_bottom if index < 2 else height - 48
            draw_vessel(canvas, center - half_width, vessel_top, center + half_width,
                        vessel_bottom, level, color)
            self.draw_sensors(canvas, index, center - half_width, center + half_width,
                              vessel_top, vessel_bottom, *tank_values[index])
            label_y = top - 10 if index < 2 else height - 12
            canvas.create_text(center, label_y, text=self.names[index].get(), width=half_width * 2,
                               fill="#243746", font=("Segoe UI", 10, "bold"))
        drain_y = height - 34
        drain_end = middle - half_width - 48
        canvas.create_line(middle, height - 48, middle, drain_y, drain_end, drain_y,
                           fill=colors[2] if self.flows[2] else inactive, width=6,
                           joinstyle="round", arrow="last", tags=("tuberia", "descarga"))
        canvas.create_text(drain_end - 8, drain_y, anchor="e",
                           text=f"Descarga {self.flows[2]:.2f} L/min", fill="#334b5c")
        self.draw_composition(canvas, middle + half_width + 18, target_top, width, colors)

    def draw_sensors(self, canvas, index, left, right, top, bottom, volume, capacity):
        for number in (1, 2):
            raw = self.fields[f"{index + 1}_s{number}_nivel"].get().strip()
            if not raw:
                continue
            try:
                threshold = nonnegative_number(raw, "El umbral")
            except ValueError:
                continue
            if capacity <= 0 or threshold > capacity:
                continue
            y = bottom - threshold / capacity * (bottom - top)
            active = volume >= threshold
            color = "#167642" if active else "#64748b"
            side = -1 if number == 1 else 1
            edge = left if side < 0 else right
            marker = edge + side * 9
            canvas.create_line(left, y, right, y, dash=(4, 3), fill=color)
            canvas.create_oval(marker - 4, y - 4, marker + 4, y + 4, outline=color,
                               fill=color if active else "#f1f5f9", tags=("sensor",))
            # Keep the mixture's composition legend clear on its right.
            inside = index == 2 and side == 1
            text = f"S{number} · {threshold:g} L"
            canvas.create_text(edge - 5 if inside else marker + side * 7, y,
                               anchor="e" if side < 0 or inside else "w", text=text,
                               fill="#243746", tags=("sensor",))

    def draw_composition(self, canvas, x, y, width, colors):
        canvas.create_text(x, y, anchor="nw", text="Composición de la mezcla",
                           fill="#243746", font=("Segoe UI", 10, "bold"), tags=("composicion",))
        if self.config is not None:
            initial = self.initial_remaining
        else:
            try:
                initial = nonnegative_number(self.fields["3_inicio"].get(), "El volumen inicial")
            except ValueError:
                initial = 0.0
        amounts = (*self.contributions, initial)
        total = sum(amounts)
        if not total:
            canvas.create_text(x, y + 21, anchor="nw", text="Depósito vacío",
                               fill="#64748b", tags=("composicion",))
            return
        names = (self.names[0].get(), self.names[1].get(), "Contenido inicial")
        for index, (name, amount, color) in enumerate(zip(names, amounts, (*colors[:2], "#64748b"))):
            if index == 2 and initial == 0:
                continue
            line_y = y + 22 + index * 17
            canvas.create_oval(x, line_y + 3, x + 8, line_y + 11, fill=color, outline="")
            canvas.create_text(x + 14, line_y, anchor="nw", width=max(90, width - x - 22),
                               text=f"{amount / total:.1%} · {name}",
                               fill="#243746", tags=("composicion",))
