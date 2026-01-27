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
import pyautogui
from Cocoa import NSApp, NSWindow, NSObject
from AppKit import (
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSStatusBar,
    NSMenu,
    NSMenuItem,
    NSPopover,
    NSViewController,
    NSView,
    NSButton,
    NSTextField,
    NSFont,
    NSColor,
    NSBorderlessWindowMask,
    NSPopoverBehaviorTransient
)
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGNullWindowID,
    CGWindowListCreateDescriptionFromArray,
    CGEventCreateMouseEvent,
    CGEventPost,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonLeft
)
from AppKit import NSWorkspace, NSRunningApplication
from Foundation import NSMakeRect, NSMakeSize
import objc


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
        """Periodically ensure window stays at correct level without stealing focus."""
        # Just ensure window level is correct - don't call raise_() as it steals focus
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

        # Show window first (without activating)
        self.show()

        # Process events to ensure window is created
        QApplication.processEvents()

        # Set window level immediately - this will keep it on top without focus
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
            import os

            # Get our own PID to exclude it
            our_pid = os.getpid()

            # Get all on-screen windows
            window_list = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID
            )

            # Find windows at this point (skip our own process)
            for window in window_list:
                # Skip our own Python process windows
                owner_pid = window.get('kCGWindowOwnerPID')
                if owner_pid == our_pid:
                    continue

                bounds = window.get('kCGWindowBounds', {})
                win_x = bounds.get('X', 0)
                win_y = bounds.get('Y', 0)
                win_width = bounds.get('Width', 0)
                win_height = bounds.get('Height', 0)

                # Check if point is within window bounds
                if (win_x <= x <= win_x + win_width and
                    win_y <= y <= win_y + win_height):

                    # Get the owner PID
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

        # Find and activate the application at the click point BEFORE closing overlay
        activated = self.find_and_activate_app_at_point(click_x, click_y)
        print(f"[DEBUG] App activation result: {activated}")

        # Stop the raise timer and close window
        self.raise_timer.stop()
        self.close()

        # Process events to ensure window is gone
        QApplication.processEvents()

        # Small delay to ensure window is fully closed and app is focused
        time.sleep(0.1)

        # Use pyautogui to click
        try:
            pyautogui.click(click_x, click_y)
            print(f"[DEBUG] Clicked at ({click_x}, {click_y}) using pyautogui")
        except Exception as e:
            print(f"[DEBUG] pyautogui click failed: {e}")

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
                QColor(67, 122, 255, 100)  # #437AFF with alpha 100
            )

        # Draw grid lines on current region
        pen = QPen(QColor(89, 90, 94, 179))  # #595A5E with 70% opacity
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


class HotkeyButton(NSButton):
    """Custom button for hotkey recording."""

    def initWithFrame_callback_(self, frame, callback):
        self = objc.super(HotkeyButton, self).initWithFrame_(frame)
        if self:
            self.callback = callback
            self.setButtonType_(0)  # Momentary push button
            self.setBordered_(True)
            self.setBezelStyle_(1)  # Rounded
            self.setTarget_(self)
            self.setAction_('buttonClicked:')
        return self

    @objc.python_method
    def buttonClicked_(self, sender):
        if self.callback:
            self.callback(self)


class SettingsView(NSView):
    """Custom view that swallows key events to prevent beeps."""

    def acceptsFirstResponder(self):
        return True

    def keyDown_(self, event):
        # Swallow all key events to prevent beep
        pass

    def keyUp_(self, event):
        # Swallow all key events
        pass


class SettingsViewController(NSViewController):
    """View controller for the settings popover."""

    def init(self):
        self = objc.super(SettingsViewController, self).init()
        if self:
            self.manager = None
            self.recording_button = None
            self.button_positions = {}  # Maps button object to (row, col)
            self.recording_modifiers = set()  # Track modifiers when recording activation hotkey
        return self

    def loadView(self):
        """Create the settings view."""
        # Create main view - smaller, more compact, using custom view to prevent beeps
        view = SettingsView.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 390))

        # 3x3 Grid of hotkey buttons - packed tightly
        self.grid_buttons = {}
        button_size = 100
        spacing = 0  # No spacing - buttons touch
        start_x = 0
        start_y = 90

        positions = [
            ('q', 0, 0), ('w', 0, 1), ('e', 0, 2),
            ('a', 1, 0), ('s', 1, 1), ('d', 1, 2),
            ('z', 2, 0), ('x', 2, 1), ('c', 2, 2),
        ]

        for key, row, col in positions:
            x = start_x + col * button_size
            y = start_y + (2 - row) * button_size  # Flip y-axis

            button = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, button_size, button_size))
            button.setTitle_(key.upper())
            button.setButtonType_(0)
            button.setBordered_(True)
            button.setBezelStyle_(4)  # Recessed bezel - square buttons that fill space
            button.setFont_(NSFont.systemFontOfSize_(24))  # Larger font
            button.setTarget_(self)
            button.setAction_('gridButtonClicked:')
            button.setIdentifier_(f"{row},{col}")  # Store position as identifier
            view.addSubview_(button)
            self.grid_buttons[(row, col)] = button
            self.button_positions[button] = (row, col)

        # Activation hotkey button (full width at top)
        self.activation_button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 45, 150, 40))
        self.activation_button.setTitle_("Ctrl + Option")
        self.activation_button.setButtonType_(0)
        self.activation_button.setBordered_(True)
        self.activation_button.setBezelStyle_(4)
        self.activation_button.setTarget_(self)
        self.activation_button.setAction_('activationButtonClicked:')
        view.addSubview_(self.activation_button)

        # Selection/Confirm key button
        self.selection_button = NSButton.alloc().initWithFrame_(NSMakeRect(150, 45, 150, 40))
        self.selection_button.setTitle_("Enter")
        self.selection_button.setButtonType_(0)
        self.selection_button.setBordered_(True)
        self.selection_button.setBezelStyle_(4)
        self.selection_button.setTarget_(self)
        self.selection_button.setAction_('selectionButtonClicked:')
        view.addSubview_(self.selection_button)

        # Quit button at bottom
        quit_button = NSButton.alloc().initWithFrame_(NSMakeRect(100, 10, 100, 30))
        quit_button.setTitle_("Quit")
        quit_button.setButtonType_(0)
        quit_button.setBordered_(True)
        quit_button.setBezelStyle_(4)
        quit_button.setTarget_(self)
        quit_button.setAction_('quitClicked:')
        view.addSubview_(quit_button)

        self.setView_(view)

    def quitClicked_(self, sender):
        """Handle quit button click."""
        # Finalize any recording in progress
        if self.recording_button == self.activation_button:
            self.finalizeActivationHotkey()
        elif self.recording_button:
            self.stopRecording()

        if self.manager:
            self.manager.quit_app()
        else:
            QApplication.quit()

    def gridButtonClicked_(self, sender):
        """Handle grid button click to record new hotkey."""
        # Prevent re-clicking while already recording
        if self.recording_button == sender:
            return

        # If recording activation hotkey, finalize it first
        if self.recording_button == self.activation_button:
            self.finalizeActivationHotkey()

        if self.recording_button:
            # Stop recording previous button
            self.stopRecording()

        # Start recording this button
        self.recording_button = sender
        old_title = sender.title()
        sender.setTitle_("...")
        sender.setEnabled_(False)

        row, col = self.button_positions.get(sender, (None, None))
        print(f"[DEBUG] Recording hotkey for position ({row}, {col})")

    def activationButtonClicked_(self, sender):
        """Handle activation button click to record new hotkey."""
        # If already recording this button, finalize it
        if self.recording_button == sender:
            self.finalizeActivationHotkey()
            return

        # If recording a different button, stop that first
        if self.recording_button:
            self.stopRecording()

        self.recording_button = sender
        self.recording_modifiers.clear()
        sender.setTitle_("Recording...")
        # Keep button enabled so user can click again to finalize

        print("[DEBUG] Recording activation hotkey - press modifiers then click again to save")

    def selectionButtonClicked_(self, sender):
        """Handle selection button click to record new hotkey."""
        # Prevent re-clicking while already recording
        if self.recording_button == sender:
            return

        # If recording activation hotkey, finalize it first
        if self.recording_button == self.activation_button:
            self.finalizeActivationHotkey()

        if self.recording_button:
            print("[DEBUG] Already recording, ignoring click")
            return

        self.recording_button = sender
        sender.setTitle_("Press key...")
        sender.setEnabled_(False)

        print("[DEBUG] Recording selection hotkey")

    @objc.python_method
    def finalizeActivationHotkey(self):
        """Finalize the activation hotkey recording with current modifiers."""
        print(f"[DEBUG] Finalizing activation hotkey with modifiers: {self.recording_modifiers}")

        if self.manager:
            if self.recording_modifiers:
                # Save the modifier combo
                self.manager.activation_modifiers = self.recording_modifiers.copy()
                self.manager.activation_key = None  # Just modifiers, no key

                # Build display string
                mod_names = [self.get_modifier_name(m) for m in sorted(self.recording_modifiers, key=str)]
                full_combo = " + ".join(mod_names)
                self.recording_button.setTitle_(full_combo)
                print(f"[DEBUG] Set activation to: {full_combo}")
            else:
                # No modifiers recorded, revert to default
                self.recording_button.setTitle_("Ctrl + Option")
                print(f"[DEBUG] No modifiers recorded, keeping default")

        self.recording_modifiers.clear()
        self.recording_button = None

    @objc.python_method
    def stopRecording(self):
        """Stop recording hotkey."""
        if self.recording_button:
            self.recording_button.setEnabled_(True)
            self.recording_button = None
        self.recording_modifiers.clear()
        print(f"[DEBUG] Stopped recording")

    @objc.python_method
    def get_modifier_name(self, key):
        """Get display name for a modifier key."""
        if key == keyboard.Key.ctrl or key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
            return "Ctrl"
        elif key == keyboard.Key.alt or key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
            return "Option"
        elif key == keyboard.Key.shift or key == keyboard.Key.shift_l or key == keyboard.Key.shift_r:
            return "Shift"
        elif key == keyboard.Key.cmd or key == keyboard.Key.cmd_l or key == keyboard.Key.cmd_r:
            return "Cmd"
        return str(key)

    @objc.python_method
    def recordKey(self, key_obj, display_name, is_modifier=False):
        """Record a key for the currently recording button.

        Args:
            key_obj: The pynput key object (Key enum or KeyCode)
            display_name: Human-readable name to display
            is_modifier: Whether this is a modifier key
        """
        if not self.recording_button:
            return

        if self.recording_button == self.activation_button:
            # Track modifiers
            if is_modifier:
                # Normalize modifier keys (ctrl_l/ctrl_r -> ctrl)
                normalized_key = key_obj
                if key_obj in [keyboard.Key.ctrl_l, keyboard.Key.ctrl_r]:
                    normalized_key = keyboard.Key.ctrl
                elif key_obj in [keyboard.Key.alt_l, keyboard.Key.alt_r]:
                    normalized_key = keyboard.Key.alt
                elif key_obj in [keyboard.Key.shift_l, keyboard.Key.shift_r]:
                    normalized_key = keyboard.Key.shift
                elif key_obj in [keyboard.Key.cmd_l, keyboard.Key.cmd_r]:
                    normalized_key = keyboard.Key.cmd

                self.recording_modifiers.add(normalized_key)

                # Show current modifiers
                mod_names = [self.get_modifier_name(m) for m in sorted(self.recording_modifiers, key=str)]
                if mod_names:
                    title = " + ".join(mod_names) + " + ..."
                    self.recording_button.setTitle_(title)
                else:
                    self.recording_button.setTitle_("Recording...")
            else:
                # Non-modifier key pressed - add to combo
                # Save the key
                if self.manager:
                    self.manager.activation_key = key_obj

                # Build display string with modifiers + key
                if self.recording_modifiers:
                    mod_names = [self.get_modifier_name(m) for m in sorted(self.recording_modifiers, key=str)]
                    full_combo = " + ".join(mod_names + [display_name])
                    self.recording_button.setTitle_(full_combo)
                else:
                    # Just the key, no modifiers
                    self.recording_button.setTitle_(display_name)
        elif self.recording_button == self.selection_button:
            # Handle selection/confirm key
            if self.manager:
                self.manager.selection_key = key_obj
                print(f"[DEBUG] Set selection key to: {display_name}")
            self.recording_button.setTitle_(display_name)
            self.stopRecording()
        else:
            # Handle grid hotkey
            row, col = self.button_positions.get(self.recording_button, (None, None))

            if row is not None and self.manager:
                # Remove any existing mapping for this position
                old_key = None
                for k, (r, c) in list(self.manager.key_map.items()):
                    if r == row and c == col:
                        old_key = k
                        del self.manager.key_map[k]
                        break

                # Add new mapping
                self.manager.key_map[key_obj] = (row, col)
                print(f"[DEBUG] Mapped {display_name} to position ({row}, {col})")

            self.recording_button.setTitle_(display_name)
            self.stopRecording()


class MenuBarManager(NSObject):
    """Manages the menu bar status item and popover."""

    def init(self):
        self = objc.super(MenuBarManager, self).init()
        if self:
            print("[DEBUG] MenuBarManager.init() called")
            self.overlay_manager = None
            self.setupMenuBar()
            print("[DEBUG] MenuBarManager.init() complete")
        return self

    @objc.python_method
    def setupMenuBar(self):
        """Setup the menu bar status item."""
        print("[DEBUG] setupMenuBar() called")
        # Create status item
        self.status_bar = NSStatusBar.systemStatusBar()
        print(f"[DEBUG] Got system status bar: {self.status_bar}")

        self.status_item = self.status_bar.statusItemWithLength_(40.0)  # Fixed width to ensure it shows
        print(f"[DEBUG] Created status item: {self.status_item}")

        # Force it to be visible by setting autosave name (this might reset hidden preference)
        try:
            self.status_item.setAutosaveName_("KeyboardNavigation")
            print("[DEBUG] Set autosave name")
        except:
            print("[DEBUG] Could not set autosave name")

        # Get the button and configure it
        button = self.status_item.button()
        if button:
            print(f"[DEBUG] Got button: {button}")
            button.setTitle_("⌨️ KB")
            print("[DEBUG] Set button title")
        else:
            # Fallback to old API
            self.status_item.setTitle_("⌨️ KB")
            print("[DEBUG] Set status item title (no button)")

        print(f"[DEBUG] Status item is visible: {self.status_item.isVisible() if hasattr(self.status_item, 'isVisible') else 'N/A'}")
        print(f"[DEBUG] Status item length: {self.status_item.length()}")

        # Create menu
        menu = NSMenu.alloc().init()

        # Configure item
        config_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Configure Hotkeys...",
            "showSettings:",
            ""
        )
        config_item.setTarget_(self)
        menu.addItem_(config_item)

        menu.addItem_(NSMenuItem.separatorItem())

        # Quit item
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit",
            "quitApp:",
            "q"
        )
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)

        self.status_item.setMenu_(menu)
        print("[DEBUG] Menu set on status item")

        # Create popover for settings
        self.popover = NSPopover.alloc().init()
        self.settings_controller = SettingsViewController.alloc().init()
        self.popover.setContentViewController_(self.settings_controller)
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        print("[DEBUG] setupMenuBar() complete")

    def showSettings_(self, sender):
        """Show the settings popover."""
        try:
            if self.popover.isShown():
                self.popover.close()
            else:
                # Update settings controller with current manager
                if self.overlay_manager:
                    self.settings_controller.manager = self.overlay_manager

                # Ensure view is loaded
                view = self.settings_controller.view()

                # Show popover relative to status item
                button = self.status_item.button()
                if button:
                    self.popover.showRelativeToRect_ofView_preferredEdge_(
                        button.bounds(),
                        button,
                        3  # NSMinYEdge (below the status item)
                    )

                    # Make the view first responder to swallow key events
                    if hasattr(view, 'window') and view.window():
                        view.window().makeFirstResponder_(view)
        except Exception as e:
            import traceback
            print(f"[DEBUG] Exception in showSettings_: {e}")
            traceback.print_exc()

    def quitApp_(self, sender):
        """Quit the application."""
        if self.overlay_manager:
            self.overlay_manager.quit_app()
        else:
            QApplication.quit()

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
        self.shift_pressed = False
        self.cmd_pressed = False

        # Key to cell mapping (row, col) - will store actual key objects
        # Initialize with default character keys
        from pynput.keyboard import KeyCode
        self.key_map = {
            KeyCode.from_char('q'): (0, 0), KeyCode.from_char('w'): (0, 1), KeyCode.from_char('e'): (0, 2),
            KeyCode.from_char('a'): (1, 0), KeyCode.from_char('s'): (1, 1), KeyCode.from_char('d'): (1, 2),
            KeyCode.from_char('z'): (2, 0), KeyCode.from_char('x'): (2, 1), KeyCode.from_char('c'): (2, 2),
        }

        # Selection/confirm key
        self.selection_key = keyboard.Key.enter

        # Activation hotkey combo
        self.activation_modifiers = {keyboard.Key.ctrl, keyboard.Key.alt}  # Default: Ctrl + Option
        self.activation_key = None  # Just modifiers, no key

        # Start global hotkey listener
        self.start_hotkey_listener()

    @staticmethod
    def get_key_display_name(key):
        """Get a human-readable display name for a key."""
        # Handle special keys
        if hasattr(key, 'name'):
            # It's a Key enum (like Key.enter, Key.shift, etc.)
            if key.name == 'enter':
                # Check if it's numpad enter by vk code
                if hasattr(key, 'vk') and key.vk == 76:  # Numpad Enter on macOS
                    return "NumEnter"
                return "Enter"
            return key.name.title()

        # Handle character keys with vk codes
        if hasattr(key, 'vk'):
            # Numpad Enter (vk 76 on macOS)
            if key.vk == 76:
                return "NumEnter"
            # Check for numpad keys (vk codes 96-105 on most systems)
            elif key.vk >= 96 and key.vk <= 105:
                return f"Num{key.vk - 96}"
            # Check for function keys
            elif key.vk >= 112 and key.vk <= 135:
                return f"F{key.vk - 111}"

        # Regular character key
        if hasattr(key, 'char') and key.char:
            return key.char.upper()

        # Fallback - show vk code if available
        if hasattr(key, 'vk'):
            return f"Key{key.vk}"
        return str(key)

    def start_hotkey_listener(self):
        """Start listening for global hotkeys."""
        def get_current_modifiers():
            """Get currently pressed modifiers as a normalized set."""
            current_mods = set()
            if self.ctrl_pressed:
                current_mods.add(keyboard.Key.ctrl)
            if self.option_pressed:
                current_mods.add(keyboard.Key.alt)
            if self.shift_pressed:
                current_mods.add(keyboard.Key.shift)
            if self.cmd_pressed:
                current_mods.add(keyboard.Key.cmd)
            return current_mods

        def check_toggle():
            """Check if the activation combo is pressed."""
            current_mods = get_current_modifiers()

            # Check if activation combo matches
            if self.activation_key is None:
                # Just modifiers, no key required
                if current_mods == self.activation_modifiers and len(current_mods) > 0:
                    if self.overlay is not None:
                        self.signals.cancel.emit()
                    else:
                        self.signals.create_and_show_overlay.emit()
            # If activation_key is set, we'd check for it too (handled in on_press)

        def on_press(key):
            # FIRST: Check if settings is recording a hotkey (highest priority)
            if hasattr(self, 'menu_bar_manager') and self.menu_bar_manager:
                controller = self.menu_bar_manager.settings_controller
                if controller.recording_button:
                    # Allow Escape to cancel recording
                    if key == keyboard.Key.esc:
                        # Restore previous value
                        if controller.recording_button == controller.activation_button:
                            controller.recording_button.setTitle_("Ctrl + Option")
                        elif controller.recording_button == controller.selection_button:
                            controller.recording_button.setTitle_("Enter")
                        controller.stopRecording()
                        return

                    # Recording mode - capture the key with display name
                    display_name = self.get_key_display_name(key)
                    is_modifier = key in [
                        keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
                        keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
                        keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r,
                        keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r
                    ]

                    # Also update our internal modifier tracking for display purposes
                    if key in [keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r]:
                        self.ctrl_pressed = True
                    elif key in [keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r]:
                        self.option_pressed = True
                    elif key in [keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r]:
                        self.shift_pressed = True
                    elif key in [keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r]:
                        self.cmd_pressed = True

                    controller.recordKey(key, display_name, is_modifier)
                    return  # Don't process normal hotkeys while recording

            # Track modifier keys (only if NOT recording)
            if key in [keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r]:
                self.ctrl_pressed = True
                check_toggle()
                return

            if key in [keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r]:  # Option key on macOS
                self.option_pressed = True
                check_toggle()
                return

            if key in [keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r]:
                self.shift_pressed = True
                check_toggle()
                return

            if key in [keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r]:
                self.cmd_pressed = True
                check_toggle()
                return

            # Check if this key completes the activation combo
            if self.activation_key is not None:
                current_mods = get_current_modifiers()

                if current_mods == self.activation_modifiers and key == self.activation_key:
                    if self.overlay is not None:
                        self.signals.cancel.emit()
                    else:
                        self.signals.create_and_show_overlay.emit()
                    return

            # Only process other keys if overlay is visible
            if self.overlay is None:
                return

            # Ctrl+Escape to quit app entirely
            if key == keyboard.Key.esc and self.ctrl_pressed:
                self.signals.quit_app.emit()
                return

            # Check for grid keys using key objects
            if key in self.key_map:
                row, col = self.key_map[key]
                self.signals.highlight_cell.emit(row, col)

            # Check for selection/confirm key
            if key == self.selection_key:
                self.signals.confirm.emit()

            # Escape to go back one level
            if key == keyboard.Key.esc:
                self.signals.go_back.emit()

        def on_release(key):
            if key in [keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r]:
                self.ctrl_pressed = False
            if key in [keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r]:
                self.option_pressed = False
            if key in [keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r]:
                self.shift_pressed = False
            if key in [keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r]:
                self.cmd_pressed = False

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
    print("  ⌨️ Menu bar icon = configure hotkeys and quit")

    app = QApplication(sys.argv)

    # Hide from dock (but keep menu bar icon)
    NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # Create the overlay manager (runs in background)
    manager = OverlayManager(monitor)

    # Create menu bar manager
    menu_bar_manager = MenuBarManager.alloc().init()
    menu_bar_manager.overlay_manager = manager
    manager.menu_bar_manager = menu_bar_manager

    print("[DEBUG] App running in background. Press Ctrl+Option to show overlay.")
    print("[DEBUG] Click ⌨️ in menu bar to configure hotkeys.")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
