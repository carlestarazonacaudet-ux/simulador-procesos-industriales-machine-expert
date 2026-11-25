"""Consulta de la guía incluida con la aplicación."""

import re
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

INLINE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)|\*\*([^*]+)\*\*|`([^`]+)`")


class HelpWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Ayuda · MachineExpertPLC")
        self.geometry("920x650")
        self.minsize(660, 420)
        self.query = tk.StringVar()
        self.feedback = tk.StringVar()
        self.match = None
        self.sections = []

        search = ttk.Frame(self, padding=(12, 10, 12, 5))
        search.pack(fill="x")
        ttk.Label(search, text="Buscar en la guía").pack(side="left")
        entry = ttk.Entry(search, textvariable=self.query, width=26)
        entry.pack(side="left", padx=7)
        ttk.Button(search, text="Siguiente", command=self.find_next).pack(side="left")
        ttk.Label(search, textvariable=self.feedback).pack(side="left", padx=8)
        entry.bind("<Return>", lambda event: self.find_next())
        self.query.trace_add("write", self.clear_search)
        self.bind("<Control-f>", lambda event: entry.focus_set())
        self.bind("<Escape>", lambda event: self.destroy())

        layout = ttk.Frame(self, padding=(12, 5, 12, 12))
        layout.pack(fill="both", expand=True)
        navigation = ttk.Frame(layout)
        navigation.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(navigation, text="Contenido").pack(anchor="w", pady=(0, 4))
        self.contents = tk.Listbox(navigation, width=28, exportselection=False, activestyle="none")
        self.contents.pack(side="left", fill="y")
        nav_scroll = ttk.Scrollbar(navigation, orient="vertical", command=self.contents.yview)
        nav_scroll.pack(side="right", fill="y")
        self.contents.configure(yscrollcommand=nav_scroll.set)
        self.contents.bind("<<ListboxSelect>>", self.goto_section)

        self.document = tk.Text(layout, wrap="word", font=("Segoe UI", 10), background="#ffffff",
                                foreground="#23384a", padx=12, pady=8, relief="flat", spacing1=2, spacing3=2)
        scroll = ttk.Scrollbar(layout, orient="vertical", command=self.document.yview)
        scroll.pack(side="right", fill="y")
        self.document.configure(yscrollcommand=scroll.set)
        self.document.pack(side="left", fill="both", expand=True)
        self.document.tag_configure("title", font=("Segoe UI", 17, "bold"), spacing3=8)
        self.document.tag_configure("section", font=("Segoe UI", 13, "bold"), spacing1=9, spacing3=4)
        self.document.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        self.document.tag_configure("code", font=("Consolas", 10), background="#edf2f6")
        self.document.tag_configure("match", background="#fff09c", foreground="#172a38")
        self.load_document()
        entry.focus_set()

    def load_document(self):
        try:
            markdown = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        except OSError:
            markdown = "# Ayuda\nNo se pudo abrir la guía incluida."
        in_code = False
        for line in markdown.splitlines():
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                self.document.insert("end", line + "\n", "code")
            elif line.startswith("# "):
                self.document.insert("end", line[2:] + "\n", "title")
            elif line.startswith("## "):
                name = line[3:]
                self.sections.append(self.document.index("end-1c"))
                self.contents.insert("end", name)
                self.document.insert("end", name + "\n", "section")
            elif line.startswith("|"):
                columns = [part.strip() for part in line.strip("|").split("|")]
                if not all(part and set(part) <= set("-: ") for part in columns):
                    self.insert_inline("  ·  ".join(columns) + "\n")
            else:
                self.insert_inline(("• " + line[2:] if line.startswith("- ") else line) + "\n")
        self.document.configure(state="disabled")

    def insert_inline(self, line):
        start = 0
        for match in INLINE.finditer(line):
            self.document.insert("end", line[start:match.start()])
            name, url, bold, code = match.groups()
            if url:
                tag = f"link_{self.document.index('end-1c')}"
                self.document.tag_configure(tag, foreground="#175ca2", underline=True)
                self.document.tag_bind(tag, "<Button-1>", lambda event, url=url: webbrowser.open(url))
                self.document.insert("end", name, tag)
            elif bold:
                self.document.insert("end", bold, "bold")
            else:
                self.document.insert("end", code, "code")
            start = match.end()
        self.document.insert("end", line[start:])

    def goto_section(self, event=None):
        selected = self.contents.curselection()
        if selected:
            self.document.yview(self.sections[selected[0]])

    def clear_search(self, *_):
        self.match = None
        self.document.tag_remove("match", "1.0", "end")
        self.feedback.set("")

    def find_next(self):
        term = self.query.get().strip()
        if not term:
            return
        start = self.match or "1.0"
        index = self.document.search(term, start, stopindex="end", nocase=True)
        if not index and self.match:
            index = self.document.search(term, "1.0", stopindex=start, nocase=True)
        self.document.tag_remove("match", "1.0", "end")
        if index:
            self.match = self.document.index(f"{index}+{len(term)}c")
            self.document.tag_add("match", index, self.match)
            self.document.see(index)
            self.feedback.set("")
        else:
            self.feedback.set("Sin coincidencias")
