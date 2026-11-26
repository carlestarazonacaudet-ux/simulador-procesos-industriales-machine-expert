"""Panel de memoria para Machine Expert mediante Modbus TCP."""

import json
import math
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from modbus import Address, LocalMemory, ProtocolError, exchange, parse_address
from simulation import TankSimulator
from mixer import MixerSimulator
from conveyor import ConveyorSimulator
from palletizer import PalletizerSimulator
from control_panel import ControlPanel
from help_view import HelpWindow
from atv320 import ATV320Simulator

DEFAULT_SCAN_MS = 1000
MAX_SCAN_MS = 2_147_483_647
PROFILE_FILES = (("Configuración JSON", "*.json"),)


@dataclass
class MemoryRow:
    address: Address
    symbol: tk.StringVar
    description: tk.StringVar
    value: tk.StringVar
    indicator: tk.Label
    editor: ttk.Widget
    controls: tuple
    widgets: tuple

    @property
    def is_bit(self):
        return self.address.coil or self.address.bit is not None


@dataclass
class Pulse:
    label: str
    request: tuple
    revision: int
    value: int
    button: ttk.Button
    compatibility: bool
    status: tk.StringVar
    previous: int | None = None
    saved_rest: int | None = None
    held: bool = True
    ready: bool = False
    closing: bool = False


class MemoryApp:
    def __init__(self, root, storage=None):
        self.root = root
        program = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        self.storage = storage or program.with_name("memorias.json")
        self.results = queue.Queue()
        self.io_lock = threading.Lock()
        self.local_memory = LocalMemory()
        self.local_mode = False
        self.rows = {}
        self.symbols = {}
        self.descriptions = {}
        self.busy = False
        self.connection_revision = 0
        self.scan_ms = DEFAULT_SCAN_MS
        self.scan_timer = None
        self.scan_in_flight = False
        self.scan_revision = 0
        self.scan_rows = deque()
        self.scan_errors = 0
        self.scan_deadline = 0
        self.pending_manual = None
        self.pulse = None
        self.help_window = None
        self.draft_save_timer = None
        self.write_drafts = {}
        root.title("Simulador de procesos industriales para Machine expert basic -autor:  Carles Tarazona -")
        if sys.platform == "win32":
            icon = str(Path(__file__).with_name("robot.ico"))
            root.iconbitmap(icon)
            root.iconbitmap(default=icon)
        root.minsize(850, 500)
        root.bind("<ButtonRelease-1>", self.release_pulse, add="+")
        root.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(root, padding=(20, 10, 20, 20))
        body.pack(fill="both", expand=True)
        heading = ttk.Frame(body)
        heading.pack(fill="x")
        self.pinned = tk.BooleanVar(value=False)
        self.pin_icon = tk.PhotoImage(master=root, width=11, height=13)
        for y, pixels in enumerate(("..#######..", "..#######..", "...#####...", "....###....",
                                    "....###....", "...#####...", "..#######..", ".#########.",
                                    ".....#.....", ".....#.....", ".....#.....", ".....#.....",
                                    ".....#.....")):
            for x, pixel in enumerate(pixels):
                if pixel == "#":
                    self.pin_icon.put("#294d64", (x, y))
        ttk.Checkbutton(
            heading, text="Fijar ventana", image=self.pin_icon, compound="left", variable=self.pinned,
            command=lambda: root.attributes("-topmost", self.pinned.get()),
        ).pack(side="left")
        ttk.Button(heading, text="Guardar como…", command=self.export_profile).pack(side="right")
        ttk.Button(heading, text="Conexión Modbus…", command=self.open_connection).pack(side="right", padx=(0, 8))
        ttk.Button(heading, text="Cargar configuración…", command=self.import_profile).pack(side="right", padx=8)
        ttk.Button(heading, text="Ayuda", command=self.open_help).pack(side="right")
        self.memory_mode_button = ttk.Button(heading, text="Memoria local: no", command=self.toggle_local_mode)
        self.memory_mode_button.pack(side="right", padx=8)
        root.bind("<F1>", lambda event: self.open_help())

        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.StringVar(value="502")
        self.unit = tk.StringVar(value="255")
        self.compatibility = tk.BooleanVar()

        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True, pady=(6, 0))
        body = ttk.Frame(tabs, padding=10)
        simulation = ttk.Frame(tabs, padding=10)
        tabs.add(body, text="Memorias")
        tabs.add(simulation, text="Simulación")
        selector = ttk.Frame(simulation)
        selector.pack(fill="x", pady=(0, 8))
        ttk.Label(selector, text="Simulador").pack(side="left")
        choice = ttk.Combobox(
            selector, values=("Depósito", "Mezcla de tres depósitos", "Cinta transportadora",
                              "Cinta de medición de altura", "Cinta de medición de ancho",
                              "Inspección por cuatro esquinas", "Paletizador cartesiano", "Botonera", "Botonera lite",
                              "Variador ATV320"),
            state="readonly", width=26,
        )
        choice.current(0)
        choice.pack(side="left", padx=8)
        tank_page = ttk.Frame(simulation)
        mixer_page = ttk.Frame(simulation)
        conveyor_page = ttk.Frame(simulation)
        height_page = ttk.Frame(simulation)
        tile_page = ttk.Frame(simulation)
        corner_page = ttk.Frame(simulation)
        pallet_page = ttk.Frame(simulation)
        control_page = ttk.Frame(simulation)
        lite_page = ttk.Frame(simulation)
        atv_page = ttk.Frame(simulation)
        self.simulator = TankSimulator(
            tank_page, self.endpoint, self.exchange_locked, self.save_simulation,
            self.compatibility,
        )
        self.mixer = MixerSimulator(
            mixer_page, self.endpoint, self.exchange_locked, self.save_simulation, self.compatibility,
        )
        self.conveyor = ConveyorSimulator(
            conveyor_page, self.endpoint, self.exchange_locked, self.save_simulation, self.compatibility,
            self.bind_pulse, self.press_address_pulse, self.write_selector,
        )
        self.height_conveyor = ConveyorSimulator(
            height_page, self.endpoint, self.exchange_locked, self.save_simulation, self.compatibility,
            self.bind_pulse, self.press_address_pulse, self.write_selector, height_mode=True,
        )
        self.tile_conveyor = ConveyorSimulator(
            tile_page, self.endpoint, self.exchange_locked, self.save_simulation, self.compatibility,
            self.bind_pulse, self.press_address_pulse, self.write_selector, tile_mode=True,
        )
        self.corner_conveyor = ConveyorSimulator(
            corner_page, self.endpoint, self.exchange_locked, self.save_simulation, self.compatibility,
            self.bind_pulse, self.press_address_pulse, self.write_selector, corner_mode=True,
        )
        self.palletizer = PalletizerSimulator(
            pallet_page, self.endpoint, self.exchange_locked, self.save_simulation, self.compatibility,
        )
        self.control_panel = ControlPanel(
            control_page, self.endpoint, self.exchange_locked, self.save_simulation,
            self.bind_pulse, self.press_address_pulse, self.write_control_sequence, lambda: not self.busy,
        )
        self.control_panel.make_lite(lite_page, self.bind_pulse)
        self.atv320 = ATV320Simulator(
            atv_page, self.endpoint, self.exchange_locked, self.save_simulation, self.compatibility,
        )
        self.profiles = {"simulacion": self.simulator, "mezcla": self.mixer,
                         "cinta": self.conveyor, "cinta_altura": self.height_conveyor,
                         "cinta_ancho": self.tile_conveyor, "cinta_esquinas": self.corner_conveyor,
                         "paletizador": self.palletizer, "botonera": self.control_panel, "atv320": self.atv320}
        for simulator in self.profiles.values():
            simulator.start_button.configure(command=lambda simulator=simulator: self.toggle_simulator(simulator))
        self.simulation_defaults = {
            key: {name: field.get() for name, field in simulator.fields.items()}
            for key, simulator in self.profiles.items()
        }
        pages = (tank_page, mixer_page, conveyor_page, height_page, tile_page, corner_page, pallet_page,
                 control_page, lite_page, atv_page)
        tank_page.pack(fill="both", expand=True)

        def resize_for_tab(event=None):
            if tabs.select() == str(simulation):
                minimum_heights = (500, 500, 590, 625, 625, 680, 665, 665, 310, 660)
                index = choice.current()
                root.minsize(680 if index == 8 else 1100, minimum_heights[index])
            else:
                root.minsize(850, 500)

        def start_lite():
            if tabs.select() != str(simulation) or choice.current() != 8 or self.control_panel.cancel is not None:
                return
            if self.busy:
                root.after(75, start_lite)
            else:
                self.control_panel.toggle()

        def show_simulator(event):
            index = choice.current()
            page_selected = pages[index]
            board_view = page_selected in (control_page, lite_page)
            for simulator in self.profiles.values():
                if simulator is not self.control_panel or not board_view:
                    simulator.pause()
            for page in pages:
                page.pack_forget()
            resize_for_tab()
            page_selected.pack(fill="both", expand=True)
            if page_selected is lite_page:
                root.after_idle(start_lite)

        choice.bind("<<ComboboxSelected>>", show_simulator)
        tabs.bind("<<NotebookTabChanged>>", resize_for_tab)

        scan = ttk.Frame(body)
        scan.pack(fill="x", pady=(0, 8))
        ttk.Label(scan, text="Escaneo (ms, 0 = pausa)").pack(side="left")
        self.scan_interval = tk.StringVar(value=str(self.scan_ms))
        interval = ttk.Entry(scan, textvariable=self.scan_interval, width=9)
        interval.pack(side="left", padx=8)
        interval.bind("<Return>", lambda event: self.apply_scan_interval())
        ttk.Button(scan, text="Aplicar", command=self.apply_scan_interval).pack(side="left")
        self.scan_status = tk.StringVar()
        ttk.Label(scan, textvariable=self.scan_status).pack(side="left", padx=10)

        add = ttk.Frame(body)
        add.pack(fill="x", pady=(0, 8))
        ttk.Label(add, text="Dirección").pack(side="left")
        self.address = tk.StringVar(value="%MW0")
        entry = ttk.Entry(add, textvariable=self.address, width=15)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda event: self.add())
        self.add_button = ttk.Button(add, text="Añadir y guardar", command=self.add)
        self.add_button.pack(side="left")
        ttk.Label(add, text="Ej. %MX200.0, %MW100, %M10").pack(side="left", padx=10)

        panel = ttk.LabelFrame(body, text="Memorias guardadas", padding=8)
        panel.pack(fill="both", expand=True)
        canvas = tk.Canvas(panel, highlightthickness=0, height=160)
        vertical = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(panel, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table = ttk.Frame(canvas, padding=4)
        canvas.create_window(0, 0, window=self.table, anchor="nw")
        self.table.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        headings = ("Dirección", "Símbolo", "Último valor", "Valor a escribir", "Pulso temporal")
        for column, title in enumerate(headings, start=1):
            ttk.Label(self.table, text=title).grid(row=0, column=column, sticky="w", padx=4)
        ttk.Label(self.table, text="Descripción").grid(row=0, column=7, sticky="w", padx=4)

        self.status = tk.StringVar(value="Añade una dirección para verla en el escaneo.")
        ttk.Label(body, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=(8, 0))
        for field in (self.host, self.port, self.unit):
            field.trace_add("write", self.clear_indicators)
        self.load()
        self.last_session = self.session_settings(self.scan_ms)
        self.restart_scan()

    @staticmethod
    def field(parent, column, label, default, width):
        value = tk.StringVar(value=default)
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w")
        ttk.Entry(parent, textvariable=value, width=width).grid(
            row=1, column=column, sticky="w", padx=(0, 12)
        )
        return value

    def open_connection(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Conexión Modbus TCP")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        controls = ttk.Frame(dialog, padding=14)
        controls.pack(fill="both", expand=True)
        host = self.field(controls, 0, "IP o nombre", self.host.get(), 22)
        port = self.field(controls, 1, "Puerto", self.port.get(), 8)
        unit = self.field(controls, 2, "ID de unidad", self.unit.get(), 12)
        compatibility = tk.BooleanVar(value=self.compatibility.get())
        ttk.Checkbutton(
            controls, text="Permitir lectura y modificación al escribir bits de palabra",
            variable=compatibility,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        feedback = tk.StringVar()
        ttk.Label(controls, textvariable=feedback, wraplength=400).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0),
        )

        def apply():
            try:
                endpoint = self.parse_endpoint(host.get(), port.get(), unit.get())
            except ValueError as error:
                feedback.set(str(error))
                return
            if self.busy:
                feedback.set("Espera a que termine la operación en curso.")
                return
            for variable, value in zip((self.host, self.port, self.unit), endpoint):
                if variable.get() != str(value):
                    variable.set(str(value))
            self.compatibility.set(compatibility.get())
            if self.draft_save_timer is not None:
                self.root.after_cancel(self.draft_save_timer)
            self.persist_session()
            dialog.destroy()

        actions = ttk.Frame(controls)
        actions.grid(row=4, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(actions, text="Cancelar", command=dialog.destroy).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Aplicar", command=apply).pack(side="left")
        dialog.bind("<Return>", lambda event: apply())
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        dialog.wait_visibility()
        dialog.grab_set()
        next(widget for widget in controls.winfo_children() if isinstance(widget, ttk.Entry)).focus_set()

    def open_help(self):
        if self.help_window is None or not self.help_window.winfo_exists():
            self.help_window = HelpWindow(self.root)
        else:
            self.help_window.deiconify()
            self.help_window.lift()
            self.help_window.focus_set()

    def load(self):
        try:
            settings, addresses = self.read_profile(self.storage)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            self.status.set(f"No se pudieron cargar las memorias guardadas. {error}")
            return
        self.local_mode = settings.get("memoria_local") is True
        self.update_memory_mode_button()
        self.apply_profile(settings, addresses)

    def read_profile(self, path):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        settings = {"direcciones": data} if isinstance(data, list) else data
        if not isinstance(settings, dict):
            raise ValueError("El archivo debe contener una configuración JSON.")
        labels = settings.get("direcciones")
        if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
            raise ValueError("La lista de direcciones no tiene el formato esperado.")
        addresses = {label: parse_address(label) for label in labels}
        if len(addresses) != len(labels):
            raise ValueError("Hay direcciones repetidas en el archivo.")
        scan_ms = settings.get("escaneo_ms", DEFAULT_SCAN_MS)
        if type(scan_ms) is not int or not 0 <= scan_ms <= MAX_SCAN_MS:
            raise ValueError("El intervalo de escaneo guardado no es válido.")
        settings["escaneo_ms"] = scan_ms
        for key in ("simbolos", "descripciones", "valores_escritura", *self.profiles):
            section = settings.get(key, {})
            if not isinstance(section, dict) or any(not isinstance(value, str) for value in section.values()):
                raise ValueError(f"La sección «{key}» no tiene el formato esperado.")
            settings[key] = section
        if "conexion" in settings:
            connection = settings["conexion"]
            if not self.valid_connection(connection):
                raise ValueError("La configuración de conexión Modbus no es válida.")
        return settings, addresses

    @staticmethod
    def valid_connection(connection):
        return (isinstance(connection, dict)
                and isinstance(connection.get("host"), str) and bool(connection["host"].strip())
                and type(connection.get("puerto")) is int and 1 <= connection["puerto"] <= 65535
                and type(connection.get("unidad")) is int and 0 <= connection["unidad"] <= 255
                and type(connection.get("compatibilidad")) is bool)

    def apply_profile(self, settings, addresses):
        self.pause_simulators()
        if self.draft_save_timer is not None:
            self.root.after_cancel(self.draft_save_timer)
            self.draft_save_timer = None
        old_rows, self.rows = self.rows, {}
        for row in old_rows.values():
            for widget in row.widgets:
                widget.destroy()
        self.symbols = {label: name for label, name in settings["simbolos"].items() if label in addresses}
        self.descriptions = {label: value for label, value in settings["descripciones"].items() if label in addresses}
        self.write_drafts = {
            label: text for label, text in settings["valores_escritura"].items()
            if label in addresses
        }
        for label, address in addresses.items():
            self.create_row(label, address)
        self.scan_ms = settings["escaneo_ms"]
        self.scan_interval.set(str(self.scan_ms))
        connection = settings.get("conexion")
        if connection is not None:
            self.host.set(connection["host"])
            self.port.set(str(connection["puerto"]))
            self.unit.set(str(connection["unidad"]))
            self.compatibility.set(connection["compatibilidad"])
        for key, simulator in self.profiles.items():
            for name, value in self.simulation_defaults[key].items():
                simulator.fields[name].set(value)
            simulator.load(settings[key])
            simulator.reset_session()
        self.layout_rows()
        self.restart_scan()
        self.last_session = self.session_settings(self.scan_ms)
        if self.write_drafts and (self.local_mode or connection is not None):
            self.restore_saved_writes()

    def profile_settings(self, labels, scan_ms):
        labels = list(labels)
        settings = {
            "direcciones": labels, "escaneo_ms": scan_ms,
            "valores_escritura": {label: self.write_drafts[label] for label in labels if self.write_drafts.get(label)},
        }
        for attribute, key in (("symbol", "simbolos"), ("description", "descripciones")):
            settings[key] = {
                label: value for label in labels if label in self.rows
                if (value := getattr(self.rows[label], attribute).get().strip())
            }
        for key, simulator in self.profiles.items():
            settings[key] = {name: field.get().strip() for name, field in simulator.fields.items()}
        try:
            host, port, unit = self.parse_endpoint(self.host.get(), self.port.get(), self.unit.get())
        except ValueError:
            pass
        else:
            settings["conexion"] = {"host": host, "puerto": port, "unidad": unit,
                                    "compatibilidad": self.compatibility.get()}
        return settings

    def session_settings(self, interval):
        settings = self.profile_settings(self.rows, interval)
        settings["memoria_local"] = self.local_mode
        return settings

    def persist_session(self):
        self.draft_save_timer = None
        settings = self.session_settings(self.scan_ms)
        if settings == self.last_session:
            return
        try:
            self.write_profile(self.storage, settings)
        except OSError as error:
            self.status.set(f"No se pudo guardar el último uso. {error}")
        else:
            self.last_session = settings

    def record_write_draft(self, label, variable):
        self.write_drafts[label] = variable.get()
        if self.draft_save_timer is not None:
            self.root.after_cancel(self.draft_save_timer)
        self.draft_save_timer = self.root.after(350, self.persist_session)

    @staticmethod
    def write_profile(path, settings):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(settings, output, indent=2, ensure_ascii=False)
                output.write("\n")
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, labels, scan_ms):
        settings = self.profile_settings(labels, scan_ms)
        settings["memoria_local"] = self.local_mode
        try:
            self.write_profile(self.storage, settings)
        except OSError as error:
            self.status.set(f"No se pudo guardar la configuración. {error}")
            return False
        self.last_session = settings
        return True

    def export_profile(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Guardar configuración como", defaultextension=".json",
            filetypes=PROFILE_FILES, initialfile="configuracion.json",
        )
        if not path:
            return
        try:
            self.endpoint()
            interval = self.number(self.scan_interval.get(), 0, MAX_SCAN_MS, "El intervalo")
            settings = self.profile_settings(self.rows, interval)
            self.write_profile(Path(path), settings)
        except (OSError, ValueError) as error:
            messagebox.showerror("Guardar configuración", str(error), parent=self.root)
            return
        messagebox.showinfo("Configuración guardada", Path(path).name, parent=self.root)

    def import_profile(self):
        if self.busy:
            messagebox.showinfo("Cargar configuración", "Espera a que termine la operación en curso.",
                                parent=self.root)
            return
        path = filedialog.askopenfilename(parent=self.root, title="Cargar configuración", filetypes=PROFILE_FILES)
        if not path:
            return
        try:
            settings, addresses = self.read_profile(Path(path))
            destination = "a la memoria local" if self.local_mode else "al PLC"
            write_notice = (f" Los valores de escritura guardados se enviarán {destination}."
                            if any(settings["valores_escritura"].values()) else "")
            accepted = messagebox.askokcancel(
                "Cargar configuración",
                f"¿Reemplazar la configuración activa por «{Path(path).name}»? "
                f"Los simuladores quedarán en pausa.{write_notice}",
                parent=self.root,
            )
            if not accepted:
                return
            if "conexion" not in settings:
                current = self.profile_settings((), self.scan_ms).get("conexion")
                if current is not None:
                    settings["conexion"] = current
            self.write_profile(self.storage, {**settings, "memoria_local": self.local_mode})
            self.apply_profile(settings, addresses)
        except (OSError, ValueError) as error:
            messagebox.showerror("Cargar configuración", str(error), parent=self.root)
            return
        self.status.set(f"Configuración cargada: {Path(path).name}")

    def save_simulation(self):
        return self.save(self.rows, self.scan_ms)

    def add(self):
        if self.busy:
            return
        label = self.address.get().strip().upper()
        try:
            address = parse_address(label)
        except ValueError as error:
            self.status.set(str(error))
            return
        duplicate = next((name for name, row in self.rows.items() if row.address == address), None)
        if duplicate:
            self.status.set(f"Esa memoria ya está guardada como {duplicate}.")
            self.rows[duplicate].editor.focus_set()
        elif self.save([*self.rows, label], self.scan_ms):
            self.create_row(label, address)
            self.layout_rows()
            self.status.set(f"{label} guardada.")
            self.restart_scan()

    def remove(self, label):
        if self.save((name for name in self.rows if name != label), self.scan_ms):
            self.symbols.pop(label, None)
            self.descriptions.pop(label, None)
            self.write_drafts.pop(label, None)
            for widget in self.rows.pop(label).widgets:
                widget.destroy()
            self.layout_rows()
            self.status.set(f"{label} quitada de la lista.")
            self.restart_scan()

    def create_row(self, label, address):
        symbol, symbol_entry = self.metadata_field(label, self.symbols, "Símbolo", 18)
        description, description_entry = self.metadata_field(
            label, self.descriptions, "Descripción", 42,
        )
        order = ttk.Frame(self.table)
        up = ttk.Button(order, text="↑", width=2, command=lambda: self.move(label, -1))
        down = ttk.Button(order, text="↓", width=2, command=lambda: self.move(label, 1))
        up.pack(side="left")
        down.pack(side="left", padx=(3, 0))
        value = tk.StringVar()
        indicator = tk.Label(self.table, text="○ Sin leer", anchor="w", fg="#555555")
        is_bit = address.coil or address.bit is not None
        if is_bit:
            editor = ttk.Combobox(
                self.table, textvariable=value, values=("Inactivo", "Activo"),
                state="readonly", width=11,
            )
            draft = self.write_drafts.get(label)
            editor.set(draft if draft in ("Activo", "Inactivo") else "Elegir…")
            editor.bind("<<ComboboxSelected>>", lambda event: self.select_write_bit(label, value))
        else:
            value.set(self.write_drafts.get(label, ""))
            value.trace_add("write", lambda *_: self.record_write_draft(label, value))
            editor = ttk.Entry(self.table, textvariable=value, width=14)
            editor.bind("<Return>", lambda event: self.write(label))
        remove = ttk.Button(self.table, text="Quitar", width=7, command=lambda: self.remove(label))
        widgets = [
            order, ttk.Label(self.table, text=label), symbol_entry, indicator, editor,
        ]
        controls = [editor, remove]
        if not is_bit:
            write = ttk.Button(self.table, text="Escribir", width=8, command=lambda: self.write(label))
            controls.append(write)
            widgets.append(write)
        else:
            pulse_controls = ttk.Frame(self.table)
            target = ttk.Combobox(
                pulse_controls, values=("Activo", "Inactivo"), state="readonly", width=9,
            )
            target.set("Activo")
            target.pack(side="left")
            button = ttk.Button(pulse_controls, text="Mantener", width=9)
            button.pack(side="left", padx=(4, 0))
            self.bind_pulse(button, lambda: self.press_pulse(label, target, button))
            widgets.append(pulse_controls)
            controls.append(target)
        widgets.extend((remove, description_entry))
        self.rows[label] = MemoryRow(
            address, symbol, description, value, indicator, editor, tuple(controls), tuple(widgets),
        )

    def metadata_field(self, label, values, title, width):
        variable = tk.StringVar(value=values.get(label, ""))
        entry = ttk.Entry(self.table, textvariable=variable, width=width)
        entry.bind("<Return>", lambda event: self.save_metadata(label, variable, values, title))
        entry.bind("<FocusOut>", lambda event: self.save_metadata(label, variable, values, title))
        return variable, entry

    def save_metadata(self, label, variable, values, title):
        if label not in self.rows:
            return
        previous = values.get(label, "")
        text = variable.get().strip()
        if text != previous:
            values[label] = text
            if self.save(self.rows, self.scan_ms):
                self.status.set(f"{title} de {label}: cambios guardados.")
            else:
                values[label] = previous

    def move(self, label, step):
        if self.busy:
            return
        labels = list(self.rows)
        index = labels.index(label)
        destination = index + step
        if 0 <= destination < len(labels):
            labels[index], labels[destination] = labels[destination], labels[index]
            if self.save(labels, self.scan_ms):
                self.rows = {name: self.rows[name] for name in labels}
                self.layout_rows()
                self.restart_scan()

    def layout_rows(self):
        for index, row in enumerate(self.rows.values(), start=1):
            for column, widget in enumerate(row.widgets):
                widget.grid(row=index, column=column, padx=4, pady=3, sticky="w")
        self.update_arrows()

    def update_arrows(self):
        for index, row in enumerate(self.rows.values()):
            up, down = row.widgets[0].winfo_children()
            up.configure(state="disabled" if self.busy or index == 0 else "normal")
            down.configure(state="disabled" if self.busy or index == len(self.rows) - 1 else "normal")

    def clear_indicators(self, *_):
        self.connection_revision += 1
        self.pause_simulators()
        self.clear_scan_values()
        self.restart_scan(delay=300)

    def update_memory_mode_button(self):
        self.memory_mode_button.configure(text=f"Memoria local: {'sí' if self.local_mode else 'no'}")

    def toggle_local_mode(self):
        if self.busy or self.pulse is not None:
            self.memory_mode_button.bell()
            return
        self.local_mode = not self.local_mode
        self.update_memory_mode_button()
        self.clear_indicators()
        self.persist_session()
        source = "Memoria local activa." if self.local_mode else "Conexión Modbus seleccionada."
        self.status.set(f"{source} Los simuladores quedaron en pausa.")

    def endpoint(self):
        if self.local_mode:
            return None, 0, 0
        return self.parse_endpoint(self.host.get(), self.port.get(), self.unit.get())

    def parse_endpoint(self, host, port_raw, unit_raw):
        host = host.strip()
        if not host:
            raise ValueError("Conexión Modbus TCP / IP o nombre = (vacío). Introduce un servidor.")
        port = self.number(port_raw, 1, 65535, f"Conexión Modbus TCP / Puerto (valor {port_raw!r})")
        unit = self.number(unit_raw, 0, 255, f"Conexión Modbus TCP / ID de unidad (valor {unit_raw!r})")
        return host, port, unit

    def apply_scan_interval(self):
        try:
            interval = self.number(self.scan_interval.get(), 0, MAX_SCAN_MS, "El intervalo")
        except ValueError as error:
            self.status.set(str(error))
            return
        if self.save(self.rows, interval):
            self.scan_ms = interval
            self.scan_interval.set(str(interval))
            self.status.set("Intervalo de escaneo guardado.")
            self.restart_scan()

    def restart_scan(self, delay=0):
        self.scan_revision += 1
        self.scan_rows.clear()
        if self.scan_timer is not None:
            self.root.after_cancel(self.scan_timer)
            self.scan_timer = None
        if not self.scan_ms:
            self.scan_status.set("Escaneo en pausa")
        elif not self.rows:
            self.scan_status.set("Sin direcciones")
        else:
            self.scan_status.set(f"Escaneo cada {self.scan_ms} ms")
            self.schedule_scan(delay)

    def schedule_scan(self, delay=0):
        if self.scan_ms and self.rows and self.scan_timer is None:
            self.scan_timer = self.root.after(delay, self.scan_next)

    def scan_next(self):
        self.scan_timer = None
        if self.busy or self.scan_in_flight:
            return
        try:
            endpoint = self.endpoint()
        except ValueError:
            self.scan_status.set("Configura la conexión")
            return
        if not self.scan_rows:
            self.scan_rows.extend(self.rows)
            self.scan_deadline = time.monotonic() + self.scan_ms / 1000
            self.scan_errors = 0
        label = self.scan_rows.popleft()
        revision = self.scan_revision
        self.scan_in_flight = True
        self.start_request(
            (*endpoint, self.rows[label].address),
            lambda value, error: self.finish_scan(label, revision, value, error),
        )

    def finish_scan(self, label, revision, value, error):
        self.scan_in_flight = False
        if self.pending_manual is not None:
            args, callback = self.pending_manual
            self.pending_manual = None
            self.start_request(args, callback)
            return
        if revision != self.scan_revision:
            self.schedule_scan()
            return
        self.show_value(self.rows[label], value)
        disconnected = isinstance(error, OSError)
        if disconnected:
            self.scan_rows.clear()
            self.clear_scan_values()
            self.scan_status.set("Escaneo sin conexión")
        elif error is not None:
            self.scan_errors += 1
        if self.scan_rows:
            self.schedule_scan()
        else:
            if not disconnected:
                self.scan_status.set(
                    f"Escaneo: {self.scan_errors} sin respuesta" if self.scan_errors
                    else f"Escaneo cada {self.scan_ms} ms"
                )
            delay = max(0, math.ceil((self.scan_deadline - time.monotonic()) * 1000))
            self.schedule_scan(delay)

    def clear_scan_values(self):
        for row in self.rows.values():
            self.show_value(row, None)

    def write(self, label):
        if self.busy:
            return
        row = self.rows[label]
        try:
            endpoint = self.endpoint()
            value = self.write_value(row, row.value.get())
        except ValueError as error:
            self.status.set(str(error))
            return

        self.set_busy(True)
        host, port, _ = endpoint
        self.status.set(f"Escribiendo {label} en {host}:{port}…")
        revision = self.connection_revision
        args = (*endpoint, row.address, value, self.compatibility.get())

        def complete(_, error):
            self.finish_write(label, value, endpoint, revision, error)

        self.prioritize_request(args, complete)

    def write_value(self, row, text):
        if row.is_bit:
            if text not in ("Activo", "Inactivo"):
                raise ValueError("Selecciona Activo o Inactivo.")
            return int(text == "Activo")
        return self.number(text, -32768, 65535, "El valor") & 0xFFFF

    def toggle_simulator(self, simulator):
        running = simulator.cancel is not None
        simulator.toggle()
        if running and simulator.cancel is None:
            self.restore_after_pause(self.connection_revision)

    def restore_after_pause(self, revision):
        if revision != self.connection_revision or any(sim.cancel is not None for sim in self.profiles.values()):
            return
        if self.busy:
            self.root.after(50, self.restore_after_pause, revision)
        elif any(row.is_bit and self.write_drafts.get(label) in ("Activo", "Inactivo")
                 for label, row in self.rows.items()):
            self.restore_saved_writes(bits_only=True)

    def restore_saved_writes(self, *, bits_only=False):
        try:
            endpoint = self.endpoint()
        except ValueError as error:
            self.status.set(str(error))
            return
        compatibility = self.compatibility.get()
        revision = self.connection_revision
        requests = deque()
        errors = []
        confirmed_words = {}
        for label, row in sorted(self.rows.items(), key=lambda item: item[1].is_bit):
            if bits_only and not row.is_bit:
                continue
            text = self.write_drafts.get(label)
            if not text:
                continue
            try:
                requests.append((label, row, self.write_value(row, text)))
            except ValueError as error:
                errors.append(f"{label}: {error}")

        def next_write():
            if revision != self.connection_revision:
                errors.append("La conexión cambió durante la restauración.")
                requests.clear()
            if not requests:
                self.set_busy(False)
                self.schedule_scan()
                self.status.set("Restauración: " + " ".join(errors) if errors else "Valores guardados aplicados al PLC.")
                return
            label, row, value = requests.popleft()

            def complete(_, error):
                if error is not None:
                    errors.append(f"{label}: escritura sin confirmar. {error}")
                    if isinstance(error, OSError):
                        if requests:
                            errors.append(f"Quedaron {len(requests)} valores sin aplicar.")
                        requests.clear()
                elif revision == self.connection_revision:
                    address = row.address
                    if address.coil:
                        self.show_value(row, value)
                    else:
                        if address.bit is None:
                            confirmed_words[address.index] = value
                        elif address.index in confirmed_words:
                            mask = 1 << address.bit
                            confirmed_words[address.index] = (confirmed_words[address.index] & ~mask) | (value << address.bit)
                        if address.index in confirmed_words:
                            word = confirmed_words[address.index]
                            for related in self.rows.values():
                                target = related.address
                                if not target.coil and target.index == address.index:
                                    self.show_value(related, word if target.bit is None else (word >> target.bit) & 1)
                        else:
                            self.show_value(row, value)
                next_write()

            self.prioritize_request((*endpoint, row.address, value, compatibility), complete)

        self.set_busy(True)
        self.status.set("Aplicando los valores guardados al PLC…")
        next_write()

    def select_write_bit(self, label, value):
        self.record_write_draft(label, value)
        self.write(label)

    def prioritize_request(self, args, callback):
        if self.scan_in_flight:
            self.pending_manual = (args, callback)
        else:
            self.start_request(args, callback)

    def write_selector(self, inactive, active, status, complete):
        self.write_control_sequence(((inactive, 0), (active, 1)), status, complete, "Selector")

    def write_control_sequence(self, operations, status, complete, title):
        operations = deque(operations)
        if not operations:
            complete(True)
            return
        if self.busy:
            status.set("Espera a que termine la operación en curso.")
            complete(False)
            return
        try:
            endpoint = self.endpoint()
        except ValueError as error:
            status.set(str(error))
            complete(False)
            return
        self.set_busy(True)
        revision = self.connection_revision
        compatibility = self.compatibility.get()
        status.set(f"{title}: aplicando valores…")

        def finish(error=None):
            self.set_busy(False)
            self.schedule_scan()
            status.set(f"{title} sin confirmar. {error}" if error else f"{title}: escritura confirmada.")
            complete(error is None)

        def advance(_, error):
            if error is not None:
                finish(error)
            elif self.connection_revision != revision:
                finish("La conexión cambió durante la operación.")
            elif not operations:
                finish()
            else:
                address, value = operations.popleft()
                self.prioritize_request((*endpoint, address, value, compatibility), advance)

        advance(None, None)

    def press_pulse(self, label, target, button):
        return self.press_address_pulse(
            label, self.rows[label].address, int(target.get() == "Activo"), button, self.status,
        )

    def bind_pulse(self, button, press):
        button.bind("<ButtonPress-1>", lambda event: press())
        button.bind("<ButtonRelease-1>", self.release_pulse)
        button.bind("<KeyPress-space>", lambda event: press())
        button.bind("<KeyRelease-space>", self.release_pulse)
        button.bind("<FocusOut>", self.release_pulse)

    def press_address_pulse(self, label, address, value, button, status, *, resting=None):
        if self.busy:
            return "break"
        try:
            endpoint = self.endpoint()
        except ValueError as error:
            status.set(str(error))
            return "break"

        button.focus_set()
        button.state(["pressed"])
        self.set_busy(True)
        pulse = Pulse(label, (*endpoint, address), self.connection_revision,
                      value, button, self.compatibility.get(), status)
        pulse.saved_rest = resting
        for name, row in self.rows.items():
            draft = self.write_drafts.get(name)
            if resting is None and row.address == address and draft in ("Activo", "Inactivo"):
                pulse.saved_rest = int(draft == "Activo")
                break
        self.pulse = pulse
        pulse.status.set(f"Leyendo el estado previo de {label}…")
        self.prioritize_request((*pulse.request, None), self.read_before_pulse)
        return "break"

    def read_before_pulse(self, previous, error):
        pulse = self.pulse
        if error is not None:
            self.end_pulse(f"No se pudo leer el estado previo de {pulse.label}. {error}", None)
            return
        if pulse.closing or self.connection_revision != pulse.revision:
            self.end_pulse(f"Pulso de {pulse.label} cancelado.", previous)
            return
        pulse.previous = previous
        if previous == pulse.value:
            self.pulse_applied(None, None)
        else:
            self.start_request((*pulse.request, pulse.value, pulse.compatibility), self.pulse_applied)

    def pulse_applied(self, _, error):
        pulse = self.pulse
        pulse.ready = True
        if error is not None:
            self.restore_pulse(f"Pulso de {pulse.label} sin confirmar. {error}")
            return
        self.update_pulse_indicator(pulse.value)
        if pulse.held:
            pulse.status.set(f"Pulso de {pulse.label} aplicado. Suelta para restaurar el estado previo.")
        else:
            self.restore_pulse()

    def release_pulse(self, event=None):
        pulse = self.pulse
        if pulse is None or not pulse.held:
            return
        if event is not None and event.type == tk.EventType.FocusOut and event.widget is not pulse.button:
            return
        pulse.held = False
        pulse.button.state(["!pressed"])
        if pulse.ready:
            self.restore_pulse()

    def restore_pulse(self, message=None):
        pulse = self.pulse
        pulse.ready = False
        resting = pulse.previous if pulse.saved_rest is None else pulse.saved_rest
        state_name = "Estado previo" if pulse.saved_rest is None else "Estado guardado"
        restored = message or f"{state_name} de {pulse.label} restaurado."
        if pulse.saved_rest is None and pulse.previous == pulse.value:
            self.end_pulse(restored, resting)
            return
        pulse.status.set(f"Restaurando el estado previo de {pulse.label}…")

        def complete(_, error):
            if error is not None:
                self.end_pulse(
                    f"No se pudo confirmar la restauración de {pulse.label} a {resting}. {error}",
                    None,
                )
            else:
                self.end_pulse(restored, resting)

        self.start_request((*pulse.request, resting, pulse.compatibility), complete)

    def update_pulse_indicator(self, value):
        pulse = self.pulse
        if self.connection_revision == pulse.revision:
            address = pulse.request[-1]
            for row in self.rows.values():
                if row.address == address:
                    self.show_value(row, value)
                elif not address.coil and not row.address.coil and row.address.index == address.index:
                    self.show_value(row, None)

    def end_pulse(self, message, value):
        pulse = self.pulse
        self.update_pulse_indicator(value)
        pulse.button.state(["!pressed"])
        self.pulse = None
        self.set_busy(False)
        pulse.status.set(message)
        if pulse.closing and value is not None:
            self.root.destroy()
        else:
            self.schedule_scan()

    def close(self):
        self.pause_simulators()
        if self.draft_save_timer is not None:
            self.root.after_cancel(self.draft_save_timer)
        self.persist_session()
        if self.pulse is None:
            self.root.destroy()
        else:
            self.pulse.closing = True
            self.release_pulse()

    def pause_simulators(self):
        for simulator in self.profiles.values():
            simulator.pause()

    def start_request(self, args, callback):
        threading.Thread(
            target=self.perform,
            args=args,
            daemon=True,
        ).start()
        self.root.after(10, self.poll, callback)

    @staticmethod
    def number(raw, lower, upper, label):
        raw = raw.strip()
        digits = raw.removeprefix("-")
        if digits.isascii() and digits.isdecimal() and len(digits) <= 10:
            value = int(raw)
            if lower <= value <= upper:
                return value
        raise ValueError(f"{label} debe ser un entero de {lower} a {upper}.")

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.add_button.configure(state=state)
        for row in self.rows.values():
            for control in row.controls:
                control.configure(
                    state="readonly" if not busy and isinstance(control, ttk.Combobox) else state
                )
        self.update_arrows()

    def perform(self, *args):
        try:
            self.results.put((self.exchange_locked(*args), None))
        except (OSError, ProtocolError) as error:
            self.results.put((None, error))

    def exchange_locked(self, *args, cancelled=None):
        with self.io_lock:
            if cancelled is not None and cancelled.is_set():
                return None
            if args[0] is None:
                return self.local_memory.exchange(*args[3:])
            return exchange(*args)

    def poll(self, callback):
        try:
            read, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(10, self.poll, callback)
            return
        callback(read, error)

    def finish_write(self, label, written, endpoint, revision, error):
        self.set_busy(False)
        self.schedule_scan()
        row = self.rows[label]
        host, port, _ = endpoint
        if error is not None:
            self.show_value(row, None)
            if isinstance(error, ConnectionRefusedError):
                message = f"Conexión rechazada en {host}:{port}. Revisa el servidor y el puerto."
            elif isinstance(error, TimeoutError):
                message = f"Tiempo de espera agotado en {host}:{port}."
            else:
                message = str(error)
            self.status.set(f"Escritura sin confirmar de {label}. {message}")
            return
        if self.connection_revision == revision:
            self.invalidate_word_aliases(row)
            self.show_value(row, written)
        self.status.set(f"Escritura de {label} confirmada por {host}:{port}.")

    def invalidate_word_aliases(self, row):
        if not row.address.coil:
            for other in self.rows.values():
                if other is not row and not other.address.coil and other.address.index == row.address.index:
                    self.show_value(other, None)

    @staticmethod
    def show_value(row, value):
        if value is None:
            row.indicator.configure(text="○ Sin leer", fg="#555555")
        elif row.is_bit:
            label = "Activo" if value else "Inactivo"
            row.indicator.configure(text=f"{'●' if value else '○'} {label}",
                                    fg="#167642" if value else "#555555")
        else:
            text = str(value) if value < 32768 else f"{value - 65536} INT / {value} WORD"
            row.indicator.configure(text=text, fg="#222222")


if __name__ == "__main__":
    root = tk.Tk()
    MemoryApp(root)
    root.mainloop()
