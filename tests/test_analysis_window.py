"""Headless checks for visible controls, movement and coalesced canvas resizing."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from hp3478a.analysis import AnalysisRenderer, PlotOptions
from hp3478a.analysis_ui import AnalysisWindow
from hp3478a.canvas import ResponsiveFigureCanvas
from hp3478a.layout import LayoutPolicy
from hp3478a.storage import CaptureData


class Surface:
    def __init__(self):
        self.options, self.grid_calls, self.callbacks, self.attributes_log = {}, [], {}, []
        self.removals = 0
        self.next_id = 0

    def configure(self, **options):
        self.options.update(options)

    def grid(self, **options):
        self.grid_calls.append(options)

    def grid_remove(self):
        self.removals += 1

    def columnconfigure(self, *args, **kwargs):
        pass

    def rowconfigure(self, *args, **kwargs):
        pass

    def after(self, delay, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = callback
        return self.next_id

    def after_cancel(self, identifier):
        self.callbacks.pop(identifier, None)

    def attributes(self, name, value):
        self.attributes_log.append((name, value))


class AnalysisWindowTests(unittest.TestCase):
    def setUp(self):
        self.analysis = AnalysisWindow.__new__(AnalysisWindow)
        self.analysis.body, self.analysis.sidebar = Surface(), Surface()
        self.plot = Surface()
        self.analysis.canvas = SimpleNamespace(get_tk_widget=lambda: self.plot)
        self.analysis.saved = Surface()
        self.analysis.window = Surface()
        self.analysis._compact = None
        self.analysis._layout_key, self.analysis._last_size, self.analysis._resize_id = (
            None,
            None,
            None,
        )
        self.analysis._fullscreen = False
        self.analysis._dpi = lambda: 1

    def test_controls_remain_visible_at_small_sizes_and_high_dpi(self):
        for width, height, scale in (
            (640, 480, 1),
            (1100, 750, 1),
            (1100, 750, 2),
            (1920, 1080, 2),
        ):
            self.analysis._layout(LayoutPolicy(width, height, scale))
            self.assertEqual(self.analysis.sidebar.removals, 0)
            self.assertTrue(self.analysis.sidebar.grid_calls)

    def test_moving_window_does_not_regrid_or_redraw_plot(self):
        self.analysis._layout(LayoutPolicy(1100, 750))
        self.analysis._last_size = (1100, 750)
        before = len(self.plot.grid_calls)
        for x in range(100):
            self.analysis._resize(
                SimpleNamespace(widget=self.analysis.window, width=1100, height=750, x=x)
            )
        self.assertEqual(len(self.plot.grid_calls), before)
        self.assertEqual(self.analysis.window.callbacks, {})
        self.analysis._layout(LayoutPolicy(1150, 780))
        self.assertEqual(len(self.plot.grid_calls), before)

    def test_controls_cannot_be_hidden(self):
        self.assertFalse(hasattr(self.analysis, "_toggle_controls"))
        self.assertFalse(hasattr(self.analysis, "controls_button"))
        self.analysis._layout(LayoutPolicy(640, 480))
        self.analysis._layout(LayoutPolicy(1400, 900))
        self.assertEqual(self.analysis.sidebar.removals, 0)
        self.assertEqual(len(self.analysis.sidebar.grid_calls), 2)

    def test_resize_events_are_coalesced(self):
        for width in range(1100, 1200):
            self.analysis._resize(
                SimpleNamespace(widget=self.analysis.window, width=width, height=750)
            )
        self.assertEqual(len(self.analysis.window.callbacks), 1)
        next(iter(self.analysis.window.callbacks.values()))()
        self.assertEqual(self.plot.grid_calls[-1], {"row": 0, "column": 1, "sticky": "nsew"})

    def test_fullscreen_can_be_entered_and_left_with_escape(self):
        self.analysis._toggle_fullscreen()
        self.assertTrue(self.analysis._fullscreen)
        self.analysis._leave_fullscreen()
        self.assertFalse(self.analysis._fullscreen)
        self.assertEqual(
            self.analysis.window.attributes_log, [("-fullscreen", True), ("-fullscreen", False)]
        )

    def test_plot_fonts_match_button_line_height_at_normal_and_high_dpi(self):
        self.analysis.renderer = AnalysisRenderer()
        self.analysis.themes = SimpleNamespace(body_font=SimpleNamespace(metrics=lambda key: 30))
        data = CaptureData((1.0, 2.0), (0.0, 1.0), "V")
        for dpi in (96, 192):
            self.analysis.renderer.figure.set_dpi(dpi)
            self.analysis._plot_resized()
            axes = self.analysis.renderer.render(data, PlotOptions(mean=True, minimum=True))
            texts = [
                axes.xaxis.label,
                axes.yaxis.label,
                axes.title,
                *axes.get_xticklabels(),
                *axes.get_yticklabels(),
                *axes.texts,
                *axes.get_legend().get_texts(),
            ]
            self.assertTrue(all(text.get_fontsize() * dpi / 72 >= 30 for text in texts))


class CanvasTests(unittest.TestCase):
    def setUp(self):
        self.canvas = ResponsiveFigureCanvas.__new__(ResponsiveFigureCanvas)
        self.widget = Surface()
        self.canvas.get_tk_widget = lambda: self.widget
        self.canvas._last_dimensions = (640, 480)
        self.canvas._resize_id, self.canvas._pending_dimensions = None, None

    def test_position_only_events_do_not_clear_the_canvas_image(self):
        with patch.object(FigureCanvasTkAgg, "resize") as resize:
            for x in range(100):
                self.canvas.resize(SimpleNamespace(width=640, height=480, x=x))
            resize.assert_not_called()
            self.assertEqual(self.widget.callbacks, {})

    def test_canvas_resizes_once_after_a_burst_of_size_events(self):
        with patch.object(FigureCanvasTkAgg, "resize") as resize:
            for width in range(641, 700):
                self.canvas.resize(SimpleNamespace(width=width, height=480))
            self.assertEqual(len(self.widget.callbacks), 1)
            next(iter(self.widget.callbacks.values()))()
            resize.assert_called_once()
            self.assertEqual(resize.call_args.args[0].width, 699)


if __name__ == "__main__":
    unittest.main()
