import sys
import subprocess
import time
from ctypes import c_void_p
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QMainWindow, QApplication
from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtGui import QPainter, QColor, QPen
from screeninfo import get_monitors
from pynput import keyboard
from pynput.mouse import Controller as MouseController, Button
from Cocoa import NSApp, NSWindow
from AppKit import (
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular
)
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGNullWindowID,
    CGWindowListCreateDescriptionFromArray
)
from AppKit import NSWorkspace, NSRunningApplication


def choose_screen():
    """Get the screen where the mouse cursor is currently located."""
    monitors = get_monitors()
    mouse = MouseController()
    mouse_x, mouse_y = mouse.position

    print(f"\nMouse position: ({mouse_x}, {mouse_y})")

    # Find which monitor contains the mouse cursor
    for monitor in monitors:
        if (monitor.x <= mouse_x < monitor.x + monitor.width and
            monitor.y <= mouse_y < monitor.y + monitor.height):
            primary = " (primary)" if monitor.is_primary else ""
            print(f"Selected screen: {monitor.name}{primary}")
            print(f"  Resolution: {monitor.width}x{monitor.height}")
            print(f"  Position: ({monitor.x}, {monitor.y})")
            return monitor

    # Fallback to primary monitor if mouse position is outside all monitors
    print("Mouse outside all monitors, using primary monitor")
    for monitor in monitors:
        if monitor.is_primary:
            return monitor

    # Last resort: return first monitor
    return monitors[0]


class HotkeySignals(QObject):
    """Signals for communicating from hotkey thread to main thread."""
    create_and_show_overlay = pyqtSignal()
    highlight_cell = pyqtSignal(int, int)  # row, col
    go_back = pyqtSignal()
    confirm = pyqtSignal()
    cancel = pyqtSignal()
    quit_app = pyqtSignal()


class GridOverlay(QMainWindow):
    def __init__(self, monitor, signals):
        super().__init__()
        self.monitor = monitor
        self.mouse = MouseController()
        self.signals = signals

        # Connect signals
        self.signals.highlight_cell.connect(self.subdivide_to_cell)
        self.signals.go_back.connect(self.go_back)
        self.signals.confirm.connect(self.confirm_selection)
        self.signals.cancel.connect(self.cancel_selection)

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
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)

        # Position on selected monitor
        self.setGeometry(monitor.x, monitor.y, monitor.width, monitor.height)

        # Timer to keep window on top
        self.raise_timer = QtCore.QTimer()
        self.raise_timer.timeout.connect(self.keep_on_top)

        # Initialize and show immediately
        self.initialize_overlay()

    def keep_on_top(self):
        """Periodically raise window to stay on top."""
        self.raise_()
        self.set_window_level_above_menubar()

    def set_window_level_above_menubar(self):
        """Set window level to appear above menu bar using PyObjC."""
        try:
            from AppKit import NSScreenSaverWindowLevel, NSMainMenuWindowLevel, NSStatusWindowLevel
            from Cocoa import NSView
            import objc

            # Use the highest practical level (screen saver level = 1000)
            # Other levels: NSStatusWindowLevel=25, NSMainMenuWindowLevel=24
            target_level = NSScreenSaverWindowLevel

            print(f"[DEBUG] Target window level: {target_level}")
            print(f"[DEBUG] NSApp windows count: {len(NSApp.windows())}")

            # Force window to be native
            win_id = self.winId()

            # Try to get NSWindow via NSView
            ns_view = objc.objc_object(c_void_p=int(win_id))
            if hasattr(ns_view, 'window'):
                ns_window = ns_view.window()
                if ns_window:
                    print(f"[DEBUG] Found NSWindow via NSView, current level: {ns_window.level()}")
                    ns_window.setLevel_(target_level)
                    ns_window.setCollectionBehavior_(
                        NSWindowCollectionBehaviorCanJoinAllSpaces |
                        NSWindowCollectionBehaviorStationary
                    )
                    ns_window.setIgnoresMouseEvents_(True)
                    ns_window.orderFrontRegardless()  # Force to front
                    print(f"[DEBUG] Set window level to {target_level}, new level: {ns_window.level()}")
                    return

            # Fallback: search through all windows
            for i, ns_window in enumerate(NSApp.windows()):
                print(f"[DEBUG] Window {i}: number={ns_window.windowNumber()}, level={ns_window.level()}, visible={ns_window.isVisible()}")
                if ns_window.windowNumber() == int(win_id):
                    print(f"[DEBUG] Found NSWindow via windowNumber, current level: {ns_window.level()}")
                    ns_window.setLevel_(target_level)
                    ns_window.setCollectionBehavior_(
                        NSWindowCollectionBehaviorCanJoinAllSpaces |
                        NSWindowCollectionBehaviorStationary
                    )
                    ns_window.setIgnoresMouseEvents_(True)
                    ns_window.orderFrontRegardless()
                    print(f"[DEBUG] Set window level to {target_level}, new level: {ns_window.level()}")
                    return

            print(f"[DEBUG] Could not find NSWindow! win_id={win_id}")

        except Exception as e:
            import traceback
            print(f"[DEBUG] Failed to set window level: {e}")
            traceback.print_exc()

    def showEvent(self, event):
        """When window is shown, set macOS window level."""
        super().showEvent(event)
        # Process events to ensure native window is created
        QApplication.processEvents()
        self.set_window_level_above_menubar()
        # Force to front
        self.activateWindow()
        self.raise_()

    def initialize_overlay(self):
        """Initialize the overlay when first created."""
        # Save original mouse position
        self.original_mouse_pos = self.mouse.position

        # Reset region to full screen
        self.region_x = 0.0
        self.region_y = 0.0
        self.region_width = float(self.monitor.width)
        self.region_height = float(self.monitor.height)
        self.region_active = False
        self.history.clear()

        # Show window first
        self.show()
        self.raise_()

        # Process events to ensure window is created
        QApplication.processEvents()

        # Set window level immediately
        self.set_window_level_above_menubar()

        # Also set it again after a tiny delay to ensure it sticks
        QtCore.QTimer.singleShot(50, self.set_window_level_above_menubar)
        QtCore.QTimer.singleShot(100, self.set_window_level_above_menubar)

        self.raise_timer.start(100)
        self.update()
        print("[DEBUG] Overlay shown")


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

    def find_and_activate_app_at_point(self, x, y):
        """Find the application at the given point and activate it."""
        try:
            # Get all on-screen windows
            window_list = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID
            )

            # Find windows at this point
            for window in window_list:
                bounds = window.get('kCGWindowBounds', {})
                win_x = bounds.get('X', 0)
                win_y = bounds.get('Y', 0)
                win_width = bounds.get('Width', 0)
                win_height = bounds.get('Height', 0)

                # Check if point is within window bounds
                if (win_x <= x <= win_x + win_width and
                    win_y <= y <= win_y + win_height):

                    # Get the owner PID
                    owner_pid = window.get('kCGWindowOwnerPID')
                    if owner_pid:
                        # Get the running application
                        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(owner_pid)
                        if app:
                            app_name = window.get('kCGWindowOwnerName', 'Unknown')
                            print(f"[DEBUG] Found app at point: {app_name} (PID: {owner_pid})")

                            # Activate the application
                            app.activateWithOptions_(0)  # 0 = NSApplicationActivateIgnoringOtherApps
                            print(f"[DEBUG] Activated {app_name}")
                            return True

            print(f"[DEBUG] No window found at ({x}, {y})")
            return False

        except Exception as e:
            import traceback
            print(f"[DEBUG] Failed to find/activate app: {e}")
            traceback.print_exc()
            return False

    def confirm_selection(self):
        """Confirm selection, close overlay, and click."""
        click_x, click_y = int(self.mouse.position[0]), int(self.mouse.position[1])
        print(f"[DEBUG] Confirming - will click at ({click_x}, {click_y})")

        # Stop the raise timer and close window
        self.raise_timer.stop()
        self.close()

        # Process events to ensure window is gone
        QApplication.processEvents()

        # Find and activate the application at the click point
        self.find_and_activate_app_at_point(click_x, click_y)

        # Perform the click using osascript
        subprocess.run([
            'osascript', '-e',
            f'tell application "System Events" to click at {{{click_x}, {click_y}}}'
        ], capture_output=True)

        print(f"[DEBUG] Clicked at ({click_x}, {click_y})")

        # Destroy this window instance completely
        self.deleteLater()

    def cancel_selection(self):
        """Cancel selection and restore mouse position."""
        if self.original_mouse_pos:
            self.mouse.position = self.original_mouse_pos

        # Stop timer and close
        self.raise_timer.stop()
        self.close()

        print("[DEBUG] Cancelled - mouse restored")

        # Destroy this window instance completely
        self.deleteLater()

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
        pen = QPen(QColor(128, 128, 128, 179))  # Gray with 70% opacity
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


class OverlayManager(QObject):
    """Manages the lifecycle of overlay windows and global hotkeys."""

    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        self.overlay = None
        self.signals = HotkeySignals()

        # Connect signals
        self.signals.create_and_show_overlay.connect(self.create_and_show_overlay)
        self.signals.quit_app.connect(self.quit_app)

        # Track modifier state
        self.ctrl_pressed = False
        self.option_pressed = False

        # Key to cell mapping (row, col)
        self.key_map = {
            'q': (0, 0), 'w': (0, 1), 'e': (0, 2),
            'a': (1, 0), 's': (1, 1), 'd': (1, 2),
            'z': (2, 0), 'x': (2, 1), 'c': (2, 2),
        }

        # Start global hotkey listener
        self.start_hotkey_listener()

    def start_hotkey_listener(self):
        """Start listening for global hotkeys."""
        def check_toggle():
            """Check if both Ctrl and Option are pressed to show/cancel overlay."""
            if self.ctrl_pressed and self.option_pressed:
                if self.overlay is not None:
                    # Cancel existing overlay
                    self.signals.cancel.emit()
                else:
                    # Create new overlay
                    self.signals.create_and_show_overlay.emit()

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
            if self.overlay is None:
                return

            # Ctrl+Escape to quit app entirely
            if key == keyboard.Key.esc and self.ctrl_pressed:
                self.signals.quit_app.emit()
                return

            # Check for grid keys
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

    def create_and_show_overlay(self):
        """Create a new overlay window."""
        if self.overlay is not None:
            return

        print("[DEBUG] Creating new overlay window")
        self.overlay = GridOverlay(self.monitor, self.signals)

        # Connect destruction signal
        self.overlay.destroyed.connect(self.on_overlay_destroyed)

    def on_overlay_destroyed(self):
        """Called when overlay window is destroyed."""
        print("[DEBUG] Overlay window destroyed")
        self.overlay = None

    def quit_app(self):
        """Quit the application entirely."""
        print("[DEBUG] Quitting app")
        self.listener.stop()
        if self.overlay:
            self.overlay.close()
        QApplication.quit()


def main():
    monitor = choose_screen()
    print(f"\nStarting on {monitor.name}...")
    print("\nControls:")
    print("  Ctrl+Option = show overlay (or cancel if already shown)")
    print("  Q/W/E/A/S/D/Z/X/C = select grid cell")
    print("  Enter = confirm and click at current position")
    print("  Escape = go back one level (or cancel if at top level)")
    print("  Ctrl+Escape = quit app")

    app = QApplication(sys.argv)

    # Hide from dock and menu bar
    NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # Create the overlay manager (runs in background)
    manager = OverlayManager(monitor)

    print("[DEBUG] App running in background. Press Ctrl+Option to show overlay.")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
