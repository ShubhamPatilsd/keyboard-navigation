import sys
import subprocess
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QMainWindow, QApplication
from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtGui import QPainter, QColor, QPen
from screeninfo import get_monitors
from pynput import keyboard
from pynput.mouse import Controller as MouseController, Button


def choose_screen():
    """List all screens and let user choose one."""
    monitors = get_monitors()

    print("\nAvailable screens:")
    print("-" * 50)
    for i, monitor in enumerate(monitors):
        print(monitor)
        primary = " (primary)" if monitor.is_primary else ""
        print(f"  [{i}] {monitor.name}{primary}")
        print(f"      Resolution: {monitor.width}x{monitor.height}")
        print(f"      Position: ({monitor.x}, {monitor.y})")
        print()

    while True:
        try:
            choice = input(f"Select screen [0-{len(monitors) - 1}]: ").strip()
            index = int(choice)
            if 0 <= index < len(monitors):
                return monitors[index]
            print(f"Please enter a number between 0 and {len(monitors) - 1}")
        except ValueError:
            print("Please enter a valid number")


class HotkeySignals(QObject):
    """Signals for communicating from hotkey thread to main thread."""
    show_overlay = pyqtSignal()
    hide_overlay = pyqtSignal()
    highlight_cell = pyqtSignal(int, int)  # row, col
    go_back = pyqtSignal()
    confirm = pyqtSignal()
    cancel = pyqtSignal()
    quit_app = pyqtSignal()


class GridOverlay(QMainWindow):
    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        self.mouse = MouseController()

        self.signals = HotkeySignals()
        self.signals.show_overlay.connect(self.show_overlay)
        self.signals.hide_overlay.connect(self.hide_overlay)
        self.signals.highlight_cell.connect(self.subdivide_to_cell)
        self.signals.go_back.connect(self.go_back)
        self.signals.confirm.connect(self.confirm_selection)
        self.signals.cancel.connect(self.cancel_selection)
        self.signals.quit_app.connect(self.quit_app)

        # Key to cell mapping (row, col)
        self.key_map = {
            'q': (0, 0), 'w': (0, 1), 'e': (0, 2),  # top row
            'a': (1, 0), 's': (1, 1), 'd': (1, 2),  # middle row
            'z': (2, 0), 'x': (2, 1), 'c': (2, 2),  # bottom row
        }

        # Current active region (starts as full screen) - use floats to avoid rounding errors
        self.region_x = 0.0
        self.region_y = 0.0
        self.region_width = float(monitor.width)
        self.region_height = float(monitor.height)
        self.region_active = False

        # History stack for going back
        self.history = []

        # Original mouse position when overlay was shown
        self.original_mouse_pos = None

        # Window setup
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint |
            Qt.FramelessWindowHint |
            Qt.Tool |
            Qt.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # Position on selected monitor
        self.setGeometry(monitor.x, monitor.y, monitor.width, monitor.height)

        # Track modifier state
        self.ctrl_pressed = False
        self.option_pressed = False
        self.overlay_visible = False
        self.cooldown = False  # Prevent immediate re-show after confirm

        # Start global hotkey listener
        self.start_hotkey_listener()

        # Timer to keep window on top (only when visible)
        self.raise_timer = QtCore.QTimer()
        self.raise_timer.timeout.connect(self.keep_on_top)

    def keep_on_top(self):
        """Periodically raise window to stay on top."""
        if self.overlay_visible:
            self.raise_()

    def showEvent(self, event):
        """When window is shown, set macOS window level."""
        super().showEvent(event)
        try:
            from AppKit import NSApp, NSScreenSaverWindowLevel
            NSApp.windows()[0].setLevel_(NSScreenSaverWindowLevel)
        except:
            pass

    def start_hotkey_listener(self):
        """Start listening for global hotkeys."""
        def check_toggle():
            """Check if both Ctrl and Option are pressed to toggle overlay."""
            if self.ctrl_pressed and self.option_pressed and not self.cooldown:
                if self.overlay_visible:
                    self.signals.cancel.emit()
                else:
                    self.signals.show_overlay.emit()

        def on_press(key):
            # Track modifier keys
            if key == keyboard.Key.ctrl:
                self.ctrl_pressed = True
                check_toggle()
                return

            if key == keyboard.Key.alt:  # Option key on macOS
                self.option_pressed = True
                check_toggle()
                return

            # Only process other keys if overlay is visible
            if not self.overlay_visible:
                return

            # Ctrl+Escape to quit app entirely
            if key == keyboard.Key.esc and self.ctrl_pressed:
                self.signals.quit_app.emit()
                return

            # Check for grid keys (no modifier needed when overlay is visible)
            try:
                if hasattr(key, 'char') and key.char in self.key_map:
                    row, col = self.key_map[key.char]
                    self.signals.highlight_cell.emit(row, col)
            except:
                pass

            # Enter to confirm selection
            if key == keyboard.Key.enter:
                self.signals.confirm.emit()

            # Escape to go back one level
            if key == keyboard.Key.esc:
                self.signals.go_back.emit()

        def on_release(key):
            if key == keyboard.Key.ctrl:
                self.ctrl_pressed = False
            if key == keyboard.Key.alt:
                self.option_pressed = False

        self.listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release
        )
        self.listener.start()

    def show_overlay(self):
        """Show the overlay and save mouse position."""
        if self.overlay_visible:
            return

        # Save original mouse position
        self.original_mouse_pos = self.mouse.position

        # Reset region to full screen
        self.region_x = 0.0
        self.region_y = 0.0
        self.region_width = float(self.monitor.width)
        self.region_height = float(self.monitor.height)
        self.region_active = False
        self.history.clear()

        # Restore window flags for stay-on-top
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint |
            Qt.FramelessWindowHint |
            Qt.Tool |
            Qt.X11BypassWindowManagerHint
        )

        # Show window
        self.overlay_visible = True
        self.show()
        self.raise_()
        self.raise_timer.start(100)
        self.update()
        print("[DEBUG] Overlay shown")

    def hide_overlay(self):
        """Hide the overlay."""
        if not self.overlay_visible:
            return

        self.overlay_visible = False
        self.raise_timer.stop()
        self.close()
        print("[DEBUG] Overlay closed")

    def subdivide_to_cell(self, row, col):
        """Subdivide current region and zoom into the specified cell."""
        # Save current state to history
        self.history.append((
            self.region_x, self.region_y,
            self.region_width, self.region_height,
            self.region_active
        ))

        # Calculate new region dimensions (1/3 of current)
        new_width = self.region_width / 3
        new_height = self.region_height / 3

        # Calculate new region position
        self.region_x = self.region_x + (col * new_width)
        self.region_y = self.region_y + (row * new_height)
        self.region_width = new_width
        self.region_height = new_height
        self.region_active = True

        # Move mouse to center of new region
        self.move_mouse_to_region_center()
        self.update()

    def go_back(self):
        """Go back one subdivision level."""
        if self.history:
            state = self.history.pop()
            self.region_x, self.region_y, self.region_width, self.region_height, self.region_active = state
            if self.region_active:
                self.move_mouse_to_region_center()
            else:
                self.mouse.position = self.original_mouse_pos
            self.update()

    def move_mouse_to_region_center(self):
        """Move mouse to center of current region."""
        center_x = self.monitor.x + self.region_x + (self.region_width / 2)
        center_y = self.monitor.y + self.region_y + (self.region_height / 2)
        self.mouse.position = (int(center_x), int(center_y))

    def confirm_selection(self):
        """Confirm selection, close overlay, and click."""
        click_x, click_y = int(self.mouse.position[0]), int(self.mouse.position[1])
        print(f"[DEBUG] Confirming - will click at ({click_x}, {click_y})")

        # Stop the raise timer first
        self.raise_timer.stop()
        self.overlay_visible = False
        self.cooldown = True  # Prevent immediate re-show

        # Close the window completely
        self.close()

        # Process events to ensure window is gone
        QApplication.processEvents()

        # Use macOS osascript to click (more reliable)
        result = subprocess.run([
            'osascript', '-e',
            f'''
            delay 0.2
            tell application "System Events"
                click at {{{click_x}, {click_y}}}
                click at {{{click_x}, {click_y}}}
            end tell
            '''
        ], capture_output=True, text=True)
        print(f"[DEBUG] osascript stdout: {result.stdout}")
        print(f"[DEBUG] osascript stderr: {result.stderr}")
        print(f"[DEBUG] osascript return code: {result.returncode}")

    def cancel_selection(self):
        """Cancel selection and restore mouse position."""
        if self.original_mouse_pos:
            self.mouse.position = self.original_mouse_pos
        self.hide_overlay()
        print("[DEBUG] Cancelled - mouse restored")

    def quit_app(self):
        """Quit the application entirely."""
        print("[DEBUG] Quitting app")
        self.listener.stop()
        QtWidgets.QApplication.quit()

    def paintEvent(self, event):
        """Draw the 3x3 grid on current region."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw highlighted region if active
        if self.region_active:
            painter.fillRect(
                int(self.region_x), int(self.region_y),
                int(self.region_width), int(self.region_height),
                QColor(255, 192, 203, 100)
            )

        # Draw grid lines on current region
        pen = QPen(QColor(255, 255, 255, 200))
        pen.setWidth(2)
        painter.setPen(pen)

        rx, ry = self.region_x, self.region_y
        rw, rh = self.region_width, self.region_height

        # Vertical lines (4 lines = 3 columns)
        for i in range(4):
            x = int(rx + (i * rw / 3))
            painter.drawLine(x, int(ry), x, int(ry + rh))

        # Horizontal lines (4 lines = 3 rows)
        for i in range(4):
            y = int(ry + (i * rh / 3))
            painter.drawLine(int(rx), y, int(rx + rw), y)


def main():
    monitor = choose_screen()
    print(f"\nStarting on {monitor.name}...")
    print("\nControls:")
    print("  Ctrl+Option = toggle overlay (show/cancel)")
    print("  Q/W/E/A/S/D/Z/X/C = select grid cell")
    print("  Enter = click at current position")
    print("  Escape = go back one level")
    print("  Ctrl+Escape = quit app")

    app = QApplication(sys.argv)
    overlay = GridOverlay(monitor)
    # Start hidden
    overlay.hide()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
