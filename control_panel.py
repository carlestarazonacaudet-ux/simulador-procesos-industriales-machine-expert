"""Frontal de mando configurable con lectura y escritura Modbus."""

import math
import queue
import threading
import tkinter as tk
from tkinter import ttk

from conveyor import ConveyorSimulator
from modbus import ProtocolError, parse_address
from simulation import AddressField, check_addresses, configured, startup_error

COLORS = {"Rojo": "#bc372f", "Verde": "#188752", "Amarillo": "#d49c17",
          "Azul": "#2775b9", "Blanco": "#d9e2eb", "Negro": "#344455"}
LITE_ROW_HEIGHTS = (46, 46, 48)
LITE_DIAL_CENTER_Y = 18


class ControlPanel:
    def __init__(self, parent, endpoint, exchange, save, bind_pulse, press_pulse, write_sequence, available):
        self.parent, self.endpoint, self.exchange, self.save = parent, endpoint, exchange, save
        self.press_pulse, self.write_sequence, self.available = press_pulse, write_sequence, available
        self.fields, self.editors, self.saved = {}, [], {}
        self.positions = [0, 0, 1, 1]
        self.selectors, self.button_widgets, self.images = [], [], []
        self.pot_elements, self.pot_values, self.display_values = [], [], []
        self.lite_buttons, self.lite_selectors, self.lite_pots = [], [], []
        self.cancel = None
        self.timer = None
        self.pot_timer = None
        self.polling = False
        self.initializing = False
        self.results = queue.Queue()
        self.pending_pots = {}
        self.status = tk.StringVar(value="Configura las direcciones debajo de los elementos y pulsa Iniciar.")
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 5))
        self.start_button = ttk.Button(bar, text="Iniciar", command=self.toggle)
        self.start_button.pack(side="left")
        ttk.Label(bar, textvariable=self.status, wraplength=920).pack(side="left", padx=10)
        self.viewport = tk.Canvas(parent, highlightthickness=0, background="#233547")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.viewport.yview)
        scrollbar.pack(side="right", fill="y")
        self.viewport.configure(yscrollcommand=scrollbar.set)
        self.viewport.pack(fill="both", expand=True)
        board = ttk.Frame(self.viewport, padding=(8, 4, 8, 10))
        panel = self.viewport.create_window(0, 0, window=board, anchor="nw")
        self.viewport.bind("<Configure>", lambda event: self.viewport.itemconfigure(panel, width=event.width))
        board.bind("<Configure>", lambda event: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        parent.winfo_toplevel().bind("<MouseWheel>", self.scroll, add="+")
        for column in range(12):
            board.columnconfigure(column, weight=1, uniform="front")
        for index in range(4):
            card = self.card(board, f"Pulsador {index + 1}", 0, index * 3, 3)
            self.make_button(card, index, bind_pulse)
            card = self.card(board, f"Selector {index + 1} · {2 if index < 2 else 3} posiciones", 1, index * 3, 3)
            self.make_selector(card, index)
        for index in range(6):
            card = self.card(board, f"Display {index + 1}", 2, index * 2, 2)
            self.make_display(card, index)
        for index in range(3):
            card = self.card(board, f"Potenciómetro {index + 1}", 3, index * 4, 4)
            self.make_pot(card, index)
        self.reset_session()

    def make_lite(self, parent, bind_pulse):
        front = tk.Frame(parent, background="#273a4c", padx=4, pady=5)
        front.pack(anchor="nw")
        buttons, selectors, pots, displays = (tk.Frame(front, background="#273a4c") for _ in range(4))
        for column, group in enumerate((buttons, selectors, pots, displays)):
            group.grid(row=0, column=column, sticky="n", padx=2)
        for index in range(4):
            cell = tk.Frame(buttons, width=70, height=70, background="#273a4c")
            cell.grid(row=index // 2, column=index % 2, padx=2, pady=2)
            button = ttk.Button(cell, compound="top", takefocus=True)
            button.place(x=0, y=0, relwidth=1, relheight=1)
            self.lite_buttons.append(button)
            bind_pulse(button, lambda index=index, button=button: self.press_button(index, button))
            face = tk.Canvas(selectors, highlightthickness=1, highlightbackground="#456077",
                             background="#273a4c", width=80, height=68,
                             cursor="hand2", takefocus=True)
            face.grid(row=index // 2, column=index % 2, padx=2, pady=2)
            self.lite_selectors.append(face)
            face.bind("<Configure>", lambda event, index=index: self.draw_selector(index))
            for event_name in ("<ButtonRelease-1>", "<Return>", "<space>"):
                face.bind(event_name, lambda event, index=index: self.change_selector(index))
        for index, value in enumerate(self.display_values):
            row = index // 2
            display = tk.Frame(displays, background="#132631", width=86, height=LITE_ROW_HEIGHTS[row])
            display.pack_propagate(False)
            display.grid(row=row, column=index % 2, padx=2, pady=(2, 2 if row == 2 else 0))
            tk.Label(display, textvariable=value, background="#132631", foreground="#87f7bf",
                     font=("Consolas", 14, "bold"), pady=0).pack(side="bottom", fill="x")
            tk.Label(display, textvariable=self.fields[f"d{index}_simbolo"], background="#132631",
                     foreground="#cae5ec", font=("Segoe UI", 8), wraplength=79,
                     pady=0).pack(side="top", fill="both", expand=True)
        for index in range(len(self.pot_values)):
            face = tk.Canvas(pots, background="#273a4c", width=86, height=LITE_ROW_HEIGHTS[index] - 2,
                             highlightthickness=1, highlightbackground="#456077",
                             cursor="hand2", takefocus=True)
            face.grid(row=index, column=0, padx=2, pady=(2, 2 if index == 2 else 0))
            self.lite_pots.append(face)
            face.bind("<Configure>", lambda event, index=index: self.draw_pot(index))
            for event_name in ("<ButtonPress-1>", "<B1-Motion>"):
                face.bind(event_name, lambda event, index=index: self.turn_lite_pot(index, event))
            face.bind("<Up>", lambda event, index=index: self.set_lite_pot(index, self.pot_values[index].get() + 1))
            face.bind("<Down>", lambda event, index=index: self.set_lite_pot(index, self.pot_values[index].get() - 1))
            self.configure_pot(index)
        ttk.Label(parent, textvariable=self.status, wraplength=575).pack(anchor="w", pady=(5, 0))
        for index in range(4):
            self.draw_button(index)
            self.draw_selector(index)

    def turn_lite_pot(self, index, event):
        x = self.lite_pots[index].winfo_width() / 2
        angle = math.degrees(math.atan2(event.x - x, LITE_DIAL_CENTER_Y - event.y))
        fraction = (max(-135.0, min(135.0, angle)) + 135) / 270
        slider = self.pot_elements[index][1]
        low, high = float(slider["from"]), float(slider["to"])
        return self.set_lite_pot(index, low + fraction * (high - low))

    def set_lite_pot(self, index, value):
        if self.cancel is None or self.initializing:
            return "break"
        slider = self.pot_elements[index][1]
        chosen = round(max(float(slider["from"]), min(float(slider["to"]), value)))
        self.pot_values[index].set(chosen)
        self.change_pot(index, chosen)
        return "break"

    def scroll(self, event):
        widget = event.widget
        while widget is not None:
            if widget is self.viewport:
                self.viewport.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            widget = widget.master

    @staticmethod
    def card(board, title, row, column, columns):
        card = ttk.LabelFrame(board, text=title, padding=5)
        card.grid(row=row, column=column, columnspan=columns, sticky="nsew", padx=3, pady=3)
        card.columnconfigure(1, weight=1)
        return card

    def field(self, card, key, label, row, default="", options=()):
        variable = tk.StringVar(value=default)
        self.fields[key] = variable
        ttk.Label(card, text=label).grid(row=row, column=0, sticky="w", padx=(0, 5))
        if options:
            editor = ttk.Combobox(card, textvariable=variable, values=options, state="readonly", width=11)
            state = "readonly"
        else:
            editor = ttk.Entry(card, textvariable=variable, width=11)
            state = "normal"
        editor.grid(row=row, column=1, sticky="ew", pady=1)
        self.editors.append((editor, state))
        return variable

    def face(self, card, height=46):
        face = tk.Canvas(card, background="#273a4c", height=height, highlightthickness=0)
        face.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        return face

    def make_button(self, card, index, bind_pulse):
        key = f"p{index}"
        button = ttk.Button(card, text="Pulsar", compound="left")
        button.grid(row=0, column=0, columnspan=2, pady=(0, 3))
        self.button_widgets.append(button)
        self.images.append(None)
        bind_pulse(button, lambda: self.press_button(index))
        self.field(card, f"{key}_direccion", "M / MW", 1)
        symbol = self.field(card, f"{key}_simbolo", "Símbolo", 2)
        color = self.field(card, f"{key}_color", "Color", 3, ("Verde", "Rojo", "Amarillo", "Azul")[index],
                           tuple(COLORS))
        self.field(card, f"{key}_inicio", "En reposo", 4, "Inactivo", ("Inactivo", "Activo"))
        for variable in (symbol, color):
            variable.trace_add("write", lambda *_: self.draw_button(index))

    def draw_button(self, index):
        key = f"p{index}"
        image = tk.PhotoImage(master=self.parent, width=32, height=32)
        chosen = COLORS.get(self.fields[f"{key}_color"].get(), COLORS["Verde"])
        for radius, color in ((15, "#203140"), (12, chosen), (5, "#e7eef2")):
            middle = 12 if radius == 5 else 15
            for offset in range(-radius, radius + 1):
                half = math.isqrt(radius * radius - offset * offset)
                y = middle + offset
                image.put(color, to=(15 - half, y, 16 + half, y + 1))
        self.images[index] = image
        label = self.fields[f"{key}_simbolo"].get().strip() or "Pulsar"
        self.button_widgets[index].configure(image=image, text=label[:24])
        if self.lite_buttons:
            lite_label = self.fields[f"{key}_simbolo"].get().strip()
            if len(lite_label) > 9:
                lite_label = lite_label[:9] + "\n" + (lite_label[9:18] if len(lite_label) <= 18 else lite_label[9:17] + "…")
            self.lite_buttons[index].configure(image=image, text=lite_label)

    def make_selector(self, card, index):
        face = self.face(card)
        self.selectors.append(face)
        face.configure(cursor="hand2", takefocus=True)
        face.bind("<Configure>", lambda event: self.draw_selector(index))
        for event in ("<ButtonRelease-1>", "<Return>", "<space>"):
            face.bind(event, lambda event: self.change_selector(index))
        key = f"s{index}"
        self.field(card, f"{key}_bit1", "Bit izquierda", 1)
        self.field(card, f"{key}_bit2", "Bit derecha", 2)
        left = self.field(card, f"{key}_izq", "Símbolo izq.", 3)
        right = self.field(card, f"{key}_der", "Símbolo der.", 4)
        fields = [left, right]
        if index >= 2:
            fields.append(self.field(card, f"{key}_centro", "Símbolo centro", 5, "0"))
        for variable in fields:
            variable.trace_add("write", lambda *_: self.draw_selector(index))

    def draw_selector(self, index):
        self.draw_selector_face(self.selectors[index], index)
        if self.lite_selectors:
            self.draw_selector_face(self.lite_selectors[index], index, compact=True)

    def draw_selector_face(self, face, index, *, compact=False):
        face.delete("all")
        x = max(80 if compact else 110, face.winfo_width()) / 2
        face.create_oval(x - 16, 6, x + 16, 38, fill="#506b81", outline="#8da3b7", width=2)
        position = self.positions[index]
        if index < 2:
            tip = (-12, 12)[position]
            parts = ("izq", "der")
        else:
            tip = (-12, 0, 12)[position]
            parts = ("izq", "centro", "der")
        face.create_line(x, 22, x + tip, 12, fill="#d7f4fd", width=4, capstyle="round")
        label = self.fields[f"s{index}_{parts[position]}"].get().strip() or ("Izq." if tip < 0 else "Der." if tip else "0")
        if compact:
            face.create_text(x, 51, text=label[:25], width=74, fill="#e6f4ff", font=("Segoe UI", 8))
        else:
            face.create_text(x + 22, 23, text=label[:15], anchor="w", fill="#e6f4ff")

    def make_display(self, card, index):
        value = tk.StringVar(value="—")
        self.display_values.append(value)
        screen = tk.Label(card, textvariable=value, background="#132631", foreground="#87f7bf",
                          font=("Consolas", 18, "bold"), height=1, width=6)
        screen.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        self.field(card, f"d{index}_direccion", "MW", 1)
        self.field(card, f"d{index}_simbolo", "Símbolo", 2)

    def make_pot(self, card, index):
        face = self.face(card)
        face.bind("<Configure>", lambda event: self.draw_pot(index))
        value = tk.DoubleVar(value=50)
        self.pot_values.append(value)
        self.fields[f"k{index}_valor"] = tk.StringVar(value="50")
        slider = ttk.Scale(card, from_=0, to=100, variable=value,
                           command=lambda raw: self.change_pot(index, raw))
        slider.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.pot_elements.append((face, slider))
        for row, suffix, label, default in ((2, "direccion", "MW", ""), (3, "simbolo", "Símbolo", ""),
                                            (4, "min", "Mínimo", "0"), (5, "max", "Máximo", "100")):
            variable = self.field(card, f"k{index}_{suffix}", label, row, default)
            if suffix == "simbolo":
                variable.trace_add("write", lambda *_: self.draw_pot(index))
            elif suffix in ("min", "max"):
                variable.trace_add("write", lambda *_: self.configure_pot(index))

    def configure_pot(self, index):
        try:
            low, high = self.pot_range(self.snapshot(), index)
            value = int(self.fields[f"k{index}_valor"].get())
        except ValueError:
            return
        self.pot_elements[index][1].configure(from_=low, to=high)
        self.pot_values[index].set(max(low, min(high, value)))
        self.draw_pot(index)

    def draw_pot(self, index):
        self.draw_pot_face(*self.pot_elements[index], index)
        if index < len(self.lite_pots):
            self.draw_pot_face(self.lite_pots[index], self.pot_elements[index][1], index, compact=True)

    def draw_pot_face(self, face, slider, index, *, compact=False):
        face.delete("all")
        x = max(86 if compact else 140, face.winfo_width()) / 2
        y, radius = (LITE_DIAL_CENTER_Y, 13) if compact else (22, 16)
        low, high = float(slider["from"]), float(slider["to"])
        value = self.pot_values[index].get()
        angle = math.radians(-135 + 270 * (value - low) / (high - low)) if high != low else 0
        face.create_oval(x - radius, y - radius, x + radius, y + radius,
                         fill="#526d84", outline="#91abbc", width=2)
        needle = radius - 4
        face.create_line(x, y, x + needle * math.sin(angle), y - needle * math.cos(angle), width=3, fill="#e6f4ff")
        symbol = self.fields[f"k{index}_simbolo"].get().strip()
        if compact:
            face.create_text(x, 38, text=f"{symbol[:9]} {round(value)}".strip(), width=84, fill="#e6f4ff",
                             font=("Segoe UI", 8))
        else:
            face.create_text(x + 23, 22, text=f"{symbol[:14]}  {round(value)}".strip(),
                             fill="#e6f4ff", anchor="w")

    @staticmethod
    def pot_range(fields, index):
        raw = (fields[f"k{index}_min"], fields[f"k{index}_max"])
        try:
            low, high = map(int, raw)
        except ValueError:
            low, high = 0, 0
        if not -32768 <= low < high <= 65535 or low < 0 < high and high > 32767:
            raise ValueError(f"Potenciómetro {index + 1}: mínimo {raw[0]!r}, máximo {raw[1]!r}. "
                             "Introduce un rango entero ascendente de 16 bits.")
        return low, high

    def snapshot(self):
        return {key: variable.get().strip() for key, variable in self.fields.items()}

    def configuration(self, fields):
        addresses, outputs = {}, []
        for index in range(4):
            key = f"p{index}_direccion"
            raw = fields[key]
            try:
                address = parse_address(raw) if raw else None
            except ValueError as error:
                raise ValueError(f"{configured(f'Pulsador {index + 1}', raw)}. {error}") from None
            addresses[key] = address
            outputs.append(AddressField(f"Pulsador {index + 1}", raw, address))
            pair = []
            for suffix in (1, 2):
                key = f"s{index}_bit{suffix}"
                label = f"Selector {index + 1} / Bit {suffix}"
                raw = fields[key]
                address = ConveyorSimulator.bit(raw, label)
                addresses[key] = address
                outputs.append(AddressField(label, raw, address))
                pair.append(address)
            if (pair[0] is None) != (pair[1] is None):
                raise ValueError(f"Selector {index + 1}: asigna los dos bits o deja ambos vacíos.")
        for prefix, total, label in (("d", 6, "Display"), ("k", 3, "Potenciómetro")):
            for index in range(total):
                key = f"{prefix}{index}_direccion"
                raw = fields[key]
                addresses[key] = ConveyorSimulator.word(raw, f"{label} {index + 1}")
                if prefix == "k":
                    self.pot_range(fields, index)
                    outputs.append(AddressField(f"{label} {index + 1}", raw, addresses[key]))
        check_addresses(outputs)
        words = [item for item in outputs if item.address is not None
                 and not item.address.coil and item.address.bit is None]
        check_addresses(words, [item for item in outputs if item not in words], shared_word=True)
        return addresses

    def load(self, settings):
        self.saved = dict(settings)
        for key, value in settings.items():
            if key in self.fields:
                self.fields[key].set(value)

    def reset_session(self):
        self.positions = [0, 0, 1, 1]
        for value in self.display_values:
            value.set("—")
        for index in range(4):
            self.draw_selector(index)
            self.draw_button(index)
        for index in range(3):
            self.configure_pot(index)

    def selector_writes(self, index, position):
        left, right = (self.addresses[f"s{index}_bit{suffix}"] for suffix in (1, 2))
        if left is None:
            return ()
        if index >= 2 and position == 1:
            return (left, 0), (right, 0)
        return ((right, 0), (left, 1)) if position == 0 else ((left, 0), (right, 1))

    def toggle(self):
        if self.cancel is not None:
            self.pause()
            return
        if not self.available():
            self.status.set("Espera a que termine la operación en curso.")
            return
        try:
            self.addresses = self.configuration(self.snapshot())
            self.target = self.endpoint() if any(self.addresses.values()) else None
        except ValueError as error:
            startup_error(self.parent, "la botonera", self.status, error)
            return
        if not self.save():
            self.status.set("No se pudo guardar el frontal.")
            return
        self.cancel = threading.Event()
        cancel = self.cancel
        self.initializing = True
        self.start_button.configure(text="Pausar", state="disabled")
        for editor, _ in self.editors:
            editor.configure(state="disabled")
        operations = []
        for index in range(4):
            address = self.addresses[f"p{index}_direccion"]
            if address is not None:
                operations.append((address, int(self.fields[f"p{index}_inicio"].get() == "Activo")))
            operations.extend(self.selector_writes(index, self.positions[index]))
        for index in range(3):
            address = self.addresses[f"k{index}_direccion"]
            if address is not None:
                operations.append((address, round(self.pot_values[index].get()) & 0xFFFF))

        def ready(success):
            if self.cancel is not cancel:
                return
            self.initializing = False
            if success:
                self.start_button.configure(state="normal")
                self.status.set("Botonera en marcha.")
                self.schedule(0)
            else:
                self.pause()

        self.write_sequence(operations, self.status, ready, "Inicio de botonera")

    def pause(self):
        if self.cancel is None:
            return
        self.cancel.set()
        self.cancel = None
        for timer in (self.timer, self.pot_timer):
            if timer is not None:
                self.parent.after_cancel(timer)
        self.timer = self.pot_timer = None
        self.pending_pots.clear()
        self.initializing = False
        self.start_button.configure(text="Iniciar", state="normal")
        for editor, state in self.editors:
            editor.configure(state=state)

    def press_button(self, index, button=None):
        if self.cancel is None or self.initializing:
            self.status.set("Pulsa Iniciar para activar la botonera.")
            return "break"
        key = f"p{index}"
        address = self.addresses[f"{key}_direccion"]
        if address is None:
            self.status.set(f"Pulsador {index + 1}: asigna una dirección.")
            return "break"
        resting = int(self.fields[f"{key}_inicio"].get() == "Activo")
        label = self.fields[f"{key}_simbolo"].get().strip() or f"Pulsador {index + 1}"
        widget = self.button_widgets[index] if button is None else button
        return self.press_pulse(label, address, 1 - resting, widget, self.status, resting=resting)

    def change_selector(self, index):
        if self.cancel is None or self.initializing or not self.available():
            return "break"
        position = (self.positions[index] + 1) % (2 if index < 2 else 3)
        cancel = self.cancel

        def complete(success):
            if success and self.cancel is cancel:
                self.positions[index] = position
                self.draw_selector(index)

        self.write_sequence(self.selector_writes(index, position), self.status, complete, f"Selector {index + 1}")
        return "break"

    def change_pot(self, index, raw):
        value = round(float(raw))
        self.fields[f"k{index}_valor"].set(str(value))
        self.draw_pot(index)
        if self.cancel is not None and self.addresses[f"k{index}_direccion"] is not None:
            self.pending_pots[index] = value & 0xFFFF
            if self.pot_timer is None:
                self.pot_timer = self.parent.after(75, self.flush_pots)

    def flush_pots(self):
        self.pot_timer = None
        if not self.pending_pots or self.cancel is None:
            return
        if not self.available() or self.initializing:
            self.pot_timer = self.parent.after(75, self.flush_pots)
            return
        operations = [(self.addresses[f"k{index}_direccion"], value)
                      for index, value in self.pending_pots.items()]
        self.pending_pots.clear()
        self.write_sequence(operations, self.status, lambda _: None, "Potenciómetros")

    def schedule(self, delay=300):
        if self.cancel is not None and not self.polling and self.timer is None:
            self.timer = self.parent.after(delay, self.sample)

    def sample(self):
        self.timer = None
        self.polling = True
        cancel = self.cancel
        addresses = tuple(self.addresses[f"d{index}_direccion"] for index in range(6))
        threading.Thread(target=self.read_displays, args=(cancel, self.target, addresses), daemon=True).start()
        self.parent.after(15, self.poll, cancel)

    def read_displays(self, cancel, endpoint, addresses):
        values, cache = [], {}
        for address in addresses:
            if address is None or cancel.is_set():
                values.append(None)
                continue
            if address not in cache:
                try:
                    cache[address] = self.exchange(*endpoint, address, cancelled=cancel)
                except (OSError, ProtocolError):
                    cache[address] = None
            values.append(cache[address])
        self.results.put(values)

    def poll(self, cancel):
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.parent.after(15, self.poll, cancel)
            return
        self.polling = False
        if self.cancel is cancel:
            for value, var in zip(result, self.display_values):
                var.set("—" if value is None else str(value))
        self.schedule(0 if self.cancel is not cancel else 300)
