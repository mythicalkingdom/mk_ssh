import tkinter as tk
import sys
import os
import subprocess
import platform
import threading
import time
import ctypes
from tkinter import ttk, filedialog, messagebox
from connect_core import load_config, list_hosts, launch_ssh


def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    # Resolve next to this script rather than the working directory, so
    # the icon is found no matter where the app is launched from.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        relative_path)


class ThinScrollbar(tk.Canvas):
    """Minimal overlay scrollbar: a thin pill-shaped thumb drawn on a
    small canvas, overlaid on the scrolled widget's edge.

    - ~10px thick (vs the ~15px ttk minimum, which cannot be changed)
    - auto-hides when the attached content fits (place/place_forget,
      so no layout shift when it appears or disappears)
    - supports click-to-jump, thumb dragging and hover highlight

    Speaks the standard scrollbar protocol: the scrolled widget's
    scrollcommand is pointed at .set(), and command is the widget's
    yview/xview.
    """

    THUMB = "#c8ccd4"
    THUMB_ACTIVE = "#9aa0a8"
    THICKNESS = 10
    MIN_THUMB = 24          # px — keep the thumb grabbable

    def __init__(self, master, orient="vertical", command=None, bg="white"):
        self.orient = orient
        self.command = command
        self._first, self._last = 0.0, 1.0
        self._visible = False
        self._drag_y = self._drag_x = 0
        self._drag_first = 0.0
        if orient == "vertical":
            w, h = self.THICKNESS, 100
        else:
            w, h = 100, self.THICKNESS
        super().__init__(master, width=w, height=h, bg=bg,
                         highlightthickness=0, bd=0, cursor="arrow")
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: self._paint(active=False))
        self.bind("<Enter>", lambda e: self._paint(active=True))
        self.bind("<Leave>", lambda e: self._paint(active=False))
        self.bind("<Configure>", lambda e: self._paint())

    # -- standard scrollbar protocol ------------------------------
    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self._relayout()

    def _needed(self):
        return self._last - self._first < 1.0

    def _relayout(self):
        if self._needed():
            if not self._visible:
                if self.orient == "vertical":
                    self.place(relx=1.0, rely=0.0, relheight=1.0,
                               anchor="e", width=self.THICKNESS)
                else:
                    self.place(relx=0.0, rely=1.0, relwidth=1.0,
                               anchor="sw", height=self.THICKNESS)
                self._visible = True
                self.update_idletasks()
            self._paint()
        elif self._visible:
            self.place_forget()
            self._visible = False

    # -- geometry helpers ------------------------------------------
    def _metrics(self):
        """Return (track_length, thumb_length, thumb_start)."""
        if self.orient == "vertical":
            length = self.winfo_height()
        else:
            length = self.winfo_width()
        span = self._last - self._first
        thumb_len = max(span * length, self.MIN_THUMB)
        track = max(length - thumb_len, 1)
        start = self._first * track
        return length, thumb_len, track, start

    def _paint(self, active=False):
        self.delete("all")
        if not self._needed():
            return
        color = self.THUMB_ACTIVE if active else self.THUMB
        _, thumb_len, _, start = self._metrics()
        if self.orient == "vertical":
            t = self.winfo_width()
            x0, x1 = 2, t - 2
            y0, y1 = start, start + thumb_len
        else:
            t = self.winfo_height()
            y0, y1 = 2, t - 2
            x0, x1 = start, start + thumb_len
        r = (x1 - x0) / 2.0
        if thumb_len >= 2 * r and r > 0:
            # pill shape: rounded caps + body
            self.create_rectangle(x0, y0 + r, x1, y1 - r,
                                  fill=color, outline=color)
            self.create_oval(x0, y0, x1, y0 + 2 * r,
                             fill=color, outline=color)
            self.create_oval(x0, y1 - 2 * r, x1, y1,
                             fill=color, outline=color)
        else:
            self.create_rectangle(x0, y0, x1, y1,
                                  fill=color, outline=color)

    # -- interaction ------------------------------------------------
    def _on_click(self, e):
        length, thumb_len, track, start = self._metrics()
        if self.orient == "vertical":
            pos, other = e.y, e.x
            thick = self.winfo_width()
        else:
            pos, other = e.x, e.y
            thick = self.winfo_height()
        in_band = 0 <= other <= thick
        on_thumb = in_band and start <= pos <= start + thumb_len
        if on_thumb:
            # begin dragging: remember the anchor
            if self.orient == "vertical":
                self._drag_y = e.y
            else:
                self._drag_x = e.x
            self._drag_first = self._first
            self._paint(active=True)
        elif in_band:
            # click on the track: jump so the thumb centers on the click
            frac = (pos - thumb_len / 2) / track
            frac = max(0.0, min(frac, 1.0 - (self._last - self._first)))
            self.command("moveto", frac)

    def _on_drag(self, e):
        length, thumb_len, track, _ = self._metrics()
        if self.orient == "vertical":
            delta = e.y - self._drag_y
        else:
            delta = e.x - self._drag_x
        frac = self._drag_first + delta / track
        frac = max(0.0, min(frac, 1.0 - (self._last - self._first)))
        self.command("moveto", frac)
        self._paint(active=True)


class SSHConnectGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SSH Connect GUI")
        icon_path = get_resource_path('mk_ssh.png')
        if os.path.exists(icon_path):
            img = tk.PhotoImage(file=icon_path)
            self.root.iconphoto(True, img)
            self._icon_img = img  # keep a reference so it isn't collected

        self.cfg = None
        self.config_path = None
        self.hosts = []
        self.selected_host = None
        self.open_group = None
        self.host_labels = {}      # name -> label widget (for in-place restyle)

        self.build_ui()
        self.load_default_config()

        # Start the background file watcher
        self._stop_watcher = False
        self._watcher_thread = threading.Thread(target=self._watch_config, daemon=True)
        self._watcher_thread.start()

    # ------------------------------------------------------------
    # UI LAYOUT
    # ------------------------------------------------------------
    def build_ui(self):
        self.root.geometry("1000x700")
        # Keep the window big enough that the sidebar (host list, Reload
        # and Config buttons) and the details panel stay usable.
        self.root.minsize(680, 420)

        main_frame = tk.Frame(self.root, bg="white")
        main_frame.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style()
        # The clam theme is used because platform default themes (e.g.
        # "vista" on Windows) draw their own scrollbars and ignore color
        # and arrow styling. Only the ttk widgets are affected (filter
        # entry and the scrollbars) — buttons and labels are classic tk.
        style.theme_use("clam")
        style.configure(
            "Red.TButton",
            background="#660000",
            foreground="white",
            padding=6,
            font=("Segoe UI", 11, "bold"),
            borderwidth=0,
            relief="flat"
        )

        # Needed on some Linux themes:
        style.map(
            "Red.TButton",
            background=[("active", "#660000")],   # darker hover
            foreground=[("active", "white")]
        )

        # Modern scrollbars are custom-drawn (see ThinScrollbar): ttk
        # scrollbars cannot be made thinner than the theme's ~15px and
        # the platform default themes ignore styling entirely.

        # Filter entry: keep it clean-looking under the clam theme
        style.configure("TEntry", fieldbackground="white",
                        bordercolor="#d0d0d0", lightcolor="#d0d0d0",
                        darkcolor="#d0d0d0", padding=4)

        # Sidebar
        sidebar = tk.Frame(main_frame, width=320, bg="white")
        sidebar.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=15, pady=(10, 15))
        sidebar.pack_propagate(False)

        # Toolbar
        toolbar = tk.Frame(sidebar, bg="white")
        toolbar.pack(fill=tk.X, pady=(0, 10), padx=0)
        tk.Button(toolbar, text="Reload", command=self.reload_config, bg="#660000", fg="white",
                  font=("Segoe UI", 11, "bold"), relief=tk.FLAT, borderwidth=0).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(toolbar, text="Config…", command=self.pick_config, bg="#660000", fg="white",
                  font=("Segoe UI", 11, "bold"), relief=tk.FLAT, borderwidth=0).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(toolbar, text="Edit", command=self.edit_config, bg="#660000", fg="white",
                  font=("Segoe UI", 11, "bold"), relief=tk.FLAT, borderwidth=0).pack(side=tk.LEFT, padx=(0, 10))

        # Filter box
        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(sidebar, textvariable=self.filter_var)
        filter_entry.pack(fill=tk.X, pady=(0, 4))
        filter_entry.bind("<KeyRelease>", lambda e: self.render_hosts())

        # Host list container: canvas + custom thin overlay scrollbar
        # that only appears when the content overflows.
        list_container = tk.Frame(sidebar, bg="white")
        list_container.pack(fill=tk.BOTH, expand=True)

        self.host_canvas = tk.Canvas(list_container, bg="white",
                                     highlightthickness=0)
        self.host_scrollbar = ThinScrollbar(list_container, orient="vertical",
                                            command=self.host_canvas.yview)
        self.host_canvas.config(yscrollcommand=self.host_scrollbar.set)
        self.host_canvas.pack(fill=tk.BOTH, expand=True)

        self.hosts_list = tk.Frame(self.host_canvas, bg="white")
        self.hosts_list.bind(
            "<Configure>",
            lambda e: self.host_canvas.configure(
                scrollregion=self.host_canvas.bbox("all")))
        self.host_canvas.create_window((0, 0), window=self.hosts_list,
                                       anchor="nw", tags="hostframe")
        # Keep the inner frame as wide as the canvas so rows stretch
        self.host_canvas.bind(
            "<Configure>",
            lambda e: self.host_canvas.itemconfigure(
                "hostframe", width=e.width))

        # Mouse wheel scrolling (Windows/macOS and Linux)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-4>", self._on_mousewheel)
        self.root.bind_all("<Button-5>", self._on_mousewheel)

        # Right panel
        right = tk.Frame(main_frame, bg="white")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=15, pady=(10, 15))

        # Top bar
        topbar = tk.Frame(right, bg="white")
        topbar.pack(fill=tk.X, pady=(0, 10), padx=0)

        self.current_config_label = tk.Label(topbar, text="Config: (none)", bg="white")
        self.current_config_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_connect = tk.Button(topbar, text="Connect", command=self.connect_selected,
                                      bg="#660000", fg="white", font=("Segoe UI", 11, "bold"), relief=tk.FLAT, borderwidth=0)
        self.btn_connect.pack(side=tk.RIGHT, padx=(20, 0))
        self.btn_connect.configure(width=12)
        self.btn_connect.config(state="disabled")

        # Details panel (with scrollbars for long values)
        details_wrapper = tk.Frame(right, bg="white", highlightthickness=1, highlightbackground="#d0d0d0")
        details_wrapper.pack(fill=tk.BOTH, expand=True)

        self.details_text = tk.Text(details_wrapper, wrap="none", font=("Consolas", 11), bg="white",
                                    relief=tk.FLAT, borderwidth=0, highlightthickness=0)

        # Configure tags for formatting
        self.details_text.tag_configure("key", foreground="#132e59", font=("Consolas", 11, "bold"))
        self.details_text.tag_configure("val", foreground="#333333")
        self.details_text.tag_configure("header", foreground="#660000", font=("Segoe UI", 12, "bold"))
        self.details_text.tag_configure("meta", foreground="#666666", font=("Consolas", 10, "italic"))

        # Custom thin overlay scrollbars (auto-hidden when content fits)
        self.details_ysb = ThinScrollbar(details_wrapper, orient="vertical",
                                         command=self.details_text.yview)
        self.details_xsb = ThinScrollbar(details_wrapper, orient="horizontal",
                                         command=self.details_text.xview)
        self.details_text.configure(yscrollcommand=self.details_ysb.set,
                                   xscrollcommand=self.details_xsb.set)
        self.details_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.details_text.insert("1.0", "Select a host…")
        self.details_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------
    # MOUSE WHEEL
    # ------------------------------------------------------------
    def _on_mousewheel(self, e):
        w = e.widget
        if w is self.details_text or w in (self.details_ysb, self.details_xsb):
            return  # the details panel and its scrollbars handle themselves
        if getattr(e, "num", None) == 4 or (hasattr(e, "delta") and e.delta > 0):
            self.host_canvas.yview_scroll(-1, "units")
        elif getattr(e, "num", None) == 5 or (hasattr(e, "delta") and e.delta < 0):
            self.host_canvas.yview_scroll(1, "units")

    # ------------------------------------------------------------
    # CONNECTION STATUS
    # ------------------------------------------------------------
    def _set_status(self, msg, duration=3000):
        self.current_config_label.config(text=msg, fg="#660000")
        if duration:
            self.root.after(duration, lambda: self.reload_status_text())

    def reload_status_text(self):
        if self.config_path:
            self.current_config_label.config(text=f"Config: {self.config_path}", fg="black")
        else:
            self.current_config_label.config(text="Config: (none)", fg="black")

    # ------------------------------------------------------------
    # FILE WATCHER
    # ------------------------------------------------------------
    def _watch_config(self):
        """
        Polls the modification time of the config file in a background thread.
        If the file changes, it schedules a reload on the main thread.
        """
        last_mtime = None
        while not self._stop_watcher:
            if self.config_path and os.path.exists(self.config_path):
                try:
                    current_mtime = os.path.getmtime(self.config_path)
                    if last_mtime is not None and current_mtime != last_mtime:
                        # File changed! Schedule reload on main thread.
                        self.root.after(0, self.reload_config)
                    last_mtime = current_mtime
                except OSError:
                    pass # File might be temporarily locked during a save
            else:
                last_mtime = None

            time.sleep(1.0) # Poll every second

    # ------------------------------------------------------------
    # CONFIG LOADING
    # ------------------------------------------------------------
    def reload_config(self):
        try:
            cfg, resolved = load_config(self.config_path)
            self.cfg = cfg
            self.config_path = resolved
            self.current_config_label.config(text=f"Config: {resolved}")
            self.hosts = list_hosts(cfg)

            # Keep the current selection if that host still exists
            names = {h["name"] for h in self.hosts}
            if self.selected_host in names:
                self.preview_host(self.selected_host)
            else:
                self.selected_host = None
                self.btn_connect.config(state="disabled")
                self.show_message("Select a host…")

            self.render_hosts()   # open_group is untouched, so the expanded group stays open
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reload config: {e}")

    def load_default_config(self):
        try:
            cfg, path = load_config(None)
            self.cfg = cfg
            self.config_path = path
            self.current_config_label.config(text=f"Config: {path}")
            self.hosts = list_hosts(cfg)
            self.selected_host = None
            self.btn_connect.config(state="disabled")
            self.show_message("Select a host…")
            self.render_hosts()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load config: {e}")

    def pick_config(self):
        path = filedialog.askopenfilename(
            title="Select SSH config file",
            filetypes=[("SSH Config", "*.config *.conf *.*")]
        )
        if not path:
            return

        try:
            cfg, resolved = load_config(path)
            self.cfg = cfg
            self.current_config_label.config(text=f"Config: {resolved}")
            self.config_path = resolved
            self.hosts = list_hosts(cfg)
            self.selected_host = None
            self.btn_connect.config(state="disabled")
            self.show_message("Select a host…")
            self.render_hosts()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load config: {e}")

    def edit_config(self):
        if not self.config_path:
            messagebox.showwarning("No Config", "No configuration file is currently loaded.")
            return

        try:
            system = platform.system().lower()
            if system == "windows":
                # Use start to open the file with the default editor
                subprocess.Popen(["cmd", "/c", "start", "", self.config_path])
            elif system == "darwin":
                # Use open on macOS
                subprocess.Popen(["open", self.config_path])
            else:
                # On Linux, xdg-open is the standard for opening files with the default app
                subprocess.Popen(["xdg-open", self.config_path])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open editor: {e}")

    # ------------------------------------------------------------
    # HOST GROUPING
    # ------------------------------------------------------------
    def group_hosts(self):
        groups = {}
        for h in self.hosts:
            g = h["group"] or "ungrouped"
            groups.setdefault(g, []).append(h)
        return groups

    # ------------------------------------------------------------
    # HOST LIST RENDERING
    # ------------------------------------------------------------
    def render_hosts(self):
        self.host_labels = {}
        for child in self.hosts_list.winfo_children():
            child.destroy()

        filter_text = self.filter_var.get().lower()
        groups = self.group_hosts()

        if not groups:
            empty_lbl = tk.Label(self.hosts_list, text="No hosts found", bg="white", fg="#666666", font=("Segoe UI", 11))
            empty_lbl.pack(pady=20)
            return

        for group_name, hosts in groups.items():
            visible = [h for h in hosts
                      if not filter_text or filter_text in h["name"].lower()]
            if not visible:
                continue   # hide groups with no matching hosts

            # Group header
            header = tk.Label(
                self.hosts_list,
                text=group_name,
                font=("Segoe UI", 14),
                fg="#132e59",
                bg="white",
                cursor="hand2",
                anchor="w",
                padx=6,
                relief=tk.FLAT,
                borderwidth=0,
                highlightthickness=0
            )
            header.pack(fill=tk.X, pady=(8, 2))

            body = tk.Frame(self.hosts_list)
            body.pack(fill=tk.X, padx=20)

            # Single-group-open behavior. While filtering, show every
            # group's matches — otherwise results could be hidden inside
            # collapsed groups.
            if self.open_group != group_name and not filter_text:
                body.pack_forget()

            def on_header_click(gn=group_name):
                self.open_group = gn if self.open_group != gn else None
                self.render_hosts()

            header.bind("<Button-1>", lambda e, gn=group_name: on_header_click(gn))

            # Host entries
            for h in visible:
                selected = h["name"] == self.selected_host

                item = tk.Label(
                    body,
                    text=h["name"],
                    anchor="w",
                    padx=8,
                    font=("Segoe UI", 12),
                    relief=tk.FLAT,
                    borderwidth=0,
                    highlightthickness=0
                )
                item.configure(bg="#0078d4" if selected else "white",
                               fg="white" if selected else "black")
                item.pack(fill=tk.X)

                def on_enter(e):
                    if e.widget["bg"] == "white":
                        e.widget.configure(bg="#f0f4ff")

                def on_leave(e):
                    if e.widget["bg"] == "#f0f4ff":
                        e.widget.configure(bg="white")

                item.bind("<Enter>", on_enter)
                item.bind("<Leave>", on_leave)

                item.bind("<Button-1>", lambda e, name=h["name"]: self.select_host(name))
                item.bind("<Double-Button-1>", lambda e, name=h["name"]: self.connect_host(name))

                self.host_labels[h["name"]] = item

    # ------------------------------------------------------------
    # HOST SELECTION
    # ------------------------------------------------------------
    def select_host(self, host):
        self.selected_host = host
        self.btn_connect.config(state="normal")
        # Restyle the existing labels in place — re-rendering here would
        # destroy the label mid-click and break double-click-to-connect.
        self._apply_selection_styles()
        self.preview_host(host)

    def _apply_selection_styles(self):
        for name, lbl in self.host_labels.items():
            selected = name == self.selected_host
            lbl.configure(bg="#0078d4" if selected else "white",
                          fg="white" if selected else "black")

    # ------------------------------------------------------------
    # DETAILS PANEL
    # ------------------------------------------------------------
    def show_message(self, msg):
        self.details_text.config(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert("1.0", msg)
        self.details_text.config(state=tk.DISABLED)

    def preview_host(self, host):
        if not self.cfg:
            return
        try:
            effective = self.cfg.get_config_for_host(host)
            self.details_text.config(state=tk.NORMAL)
            self.details_text.delete("1.0", tk.END)

            host_node = self.cfg.find_host_node(host)
            if host_node:
                self.details_text.insert("end", f"Host: {host}\n", "header")
                self.details_text.insert("end", "\n")
                self.details_text.insert("end", "Defined at:\n", "key")
                self.details_text.insert("end", f"{host_node.source_file}:{host_node.source_line}\n", "meta")
                self.details_text.insert("end", f"Host pattern: {' '.join(host_node.patterns)}\n", "meta")
                self.details_text.insert("end", "\n")

            # Sort keys for consistent display
            for k in sorted(effective.keys()):
                v = effective[k]
                if isinstance(v, list):
                    v_str = " ".join(str(x) for x in v)
                else:
                    v_str = str(v)

                self.details_text.insert("end", f"{k}: ", "key")
                self.details_text.insert("end", f"{v_str}\n", "val")

            if not effective and not host_node:
                self.details_text.insert("end", "(no directives)")

            self.details_text.config(state=tk.DISABLED)

        except NotImplementedError as e:
            self.show_message(
                f"Preview not available for this host.\n{e}\n\n"
                "Connect will hand this host to ssh with the selected "
                "config file, which can evaluate it fully.")
        except Exception as e:
            self.show_message(f"Error: {e}")

    # ------------------------------------------------------------
    # SSH LAUNCH
    # ------------------------------------------------------------
    def connect_host(self, host):
        if not self.cfg:
            return
        try:
            # Give the user immediate feedback that the connection is launching
            self._set_status(f"Launching connection to {host}...")

            # config_path is passed through so the launcher uses the
            # selected config (e.g. -F on the Match fallback), not ssh's
            # default one.
            launch_ssh(host, self.cfg, self.config_path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch SSH: {e}")

    def connect_selected(self):
        if self.selected_host:
            self.connect_host(self.selected_host)


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
if __name__ == "__main__":
    # Enable High DPI awareness for Windows to prevent blurry windows and icons
    if platform.system() == "Windows":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    root = tk.Tk()
    app = SSHConnectGUI(root)
    root.mainloop()
