# -*- coding: utf-8 -*-

"""
Data Processing and Statistical Plot Rendering Core.

This module encapsulates the analytical engine for the distinct plot views:
Line, Histogram, and KDE plots. It computes summary statistics, performs 
kernel density estimation, and draws custom analytical overlays.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import statistics
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from matplotlib import colors, rc_context
from matplotlib.figure import Figure
from scipy.stats import gaussian_kde

# Local Packages
from .storage import CaptureData

PLOT_TYPES = ("Line", "Histogram", "KDE", "Box", "Violin")


@dataclass(frozen=True)
class PlotOptions:
    kind: str = "Line"
    title: str = "Acquisition"
    color: str = "red"
    grid: bool = True
    mean: bool = False
    std: bool = False
    median: bool = False
    mode: bool = False
    minimum: bool = False
    maximum: bool = False
    percentage: bool = False
    kde: bool = False
    vertical: bool = False
    quartiles: bool = False
    bins: int | None = None
    x_limits: tuple[float, float] | None = None
    y_limits: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.kind not in PLOT_TYPES or not colors.is_color_like(self.color):
            raise ValueError("Select a valid plot type and color.")
        if self.bins is not None and (not isinstance(self.bins, int) or not 10 <= self.bins <= 200):
            raise ValueError("Histogram intervals must be from 10 to 200 (empty for Auto).")
        for name, bounds in (("X", self.x_limits), ("Y", self.y_limits)):
            if bounds is not None and (
                len(bounds) != 2
                or not all(math.isfinite(value) for value in bounds)
                or bounds[0] >= bounds[1]
            ):
                raise ValueError(
                    f"{name} lower limit must be less than the upper limit; both must be finite."
                )


@dataclass(frozen=True)
class CaptureStatistics:
    count: int
    minimum: float
    maximum: float
    mean: float
    std: float
    median: float


class DataAnalyzer:
    def visible_data(
        self,
        data: CaptureData,
        x_limits: tuple[float, float] | None = None,
        y_limits: tuple[float, float] | None = None,
    ) -> CaptureData | None:
        """Select original samples inside every specified bound, including its edges."""
        if x_limits is None and y_limits is None:
            return data
        samples = [
            (value, timestamp)
            for value, timestamp in zip(data.values, data.timestamps)
            if (x_limits is None or x_limits[0] <= timestamp <= x_limits[1])
            and (y_limits is None or y_limits[0] <= value <= y_limits[1])
        ]
        if not samples:
            return None
        values, timestamps = zip(*samples)
        return CaptureData(values, timestamps, data.unit, data.title)

    def statistics(self, data: CaptureData) -> CaptureStatistics:
        values = data.values
        return CaptureStatistics(
            len(values),
            min(values),
            max(values),
            statistics.mean(values),
            statistics.stdev(values) if len(values) > 1 else 0.0,
            statistics.median(values),
        )

    def density(self, data: CaptureData, points: int = 1000) -> tuple[np.ndarray, np.ndarray]:
        if len(data.values) < 2 or max(data.values) == min(data.values):
            raise ValueError("A density curve needs at least two distinct values.")
        values = np.asarray(data.values)
        kernel = gaussian_kde(values)
        padding = 3 * float(np.sqrt(kernel.covariance[0, 0]))
        coordinates = np.linspace(values.min() - padding, values.max() + padding, points)
        return coordinates, kernel(coordinates)


class AnalysisRenderer:
    """Keep analysis figures separate from the live acquisition figure."""

    def __init__(self, figure: Figure | None = None, *, font_size: float = 12) -> None:
        self.figure = figure or Figure(figsize=(10, 6), dpi=96, layout="constrained")
        self.font_size = font_size
        self._data, self._options = None, None
        self.last_statistics: CaptureStatistics | None = None
        self.analyzer = DataAnalyzer()

    def render(self, data: CaptureData, options: PlotOptions):
        self._data, self._options = data, options
        self.figure.clear()
        axes = self.figure.add_subplot(111)
        values, color = np.asarray(data.values), options.color
        statistics_data = (
            self.analyzer.visible_data(data, options.x_limits, options.y_limits)
            if options.kind == "Line"
            else data
        )
        stats = self.analyzer.statistics(statistics_data) if statistics_data is not None else None
        self.last_statistics = stats
        axes.set_title(options.title, wrap=True)
        if options.kind == "Line":
            axes.plot(
                data.timestamps,
                values,
                color=color,
                linewidth=1.5,
                marker="." if len(values) == 1 else None,
            )
            axes.set(xlabel="Time [s]", ylabel=f"Measurement [{data.unit}]")
            if statistics_data is None:
                axes.text(
                    0.5,
                    0.95,
                    "No samples in the selected area.",
                    transform=axes.transAxes,
                    ha="center",
                    va="top",
                    wrap=True,
                )
            else:
                visible_values = np.asarray(statistics_data.values)
                for enabled, index, label in (
                    (options.minimum, int(visible_values.argmin()), "Min"),
                    (options.maximum, int(visible_values.argmax()), "Max"),
                ):
                    if enabled:
                        axes.plot(
                            statistics_data.timestamps[index],
                            visible_values[index],
                            "o",
                            color=color,
                        )
                        axes.annotate(
                            f"{label}: {visible_values[index]:.6g}",
                            (statistics_data.timestamps[index], visible_values[index]),
                            xytext=(4, 8),
                            textcoords="offset points",
                        )
                if options.mean:
                    axes.axhline(
                        stats.mean, color=color, linestyle="--", label=f"Mean: {stats.mean:.6g}"
                    )
                    if options.std:
                        axes.axhspan(
                            stats.mean - stats.std,
                            stats.mean + stats.std,
                            color=color,
                            alpha=0.15,
                            label=f"SD: {stats.std:.6g}",
                        )
        elif options.kind == "Histogram":
            bins = options.bins or min(100, max(20, round(len(values) / 2)))
            weights = np.full(len(values), 100 / len(values)) if options.percentage else None
            _counts, edges, _ = axes.hist(
                values, bins=bins, weights=weights, color=color, edgecolor="white"
            )
            axes.set(
                xlabel=f"Measurement [{data.unit}]",
                ylabel="Distribution [%]" if options.percentage else "Count",
            )
            if options.kde:
                try:
                    x, density = self.analyzer.density(data)
                    scale = (100 if options.percentage else len(values)) * (edges[1] - edges[0])
                    axes.plot(x, density * scale, color=color, linewidth=2)
                except ValueError:
                    self._density_note(axes)
        elif options.kind == "KDE":
            axes.set(xlabel=f"Measurement [{data.unit}]", ylabel="Density")
            try:
                x, density = self.analyzer.density(data)
                axes.plot(x, density, color=color, linewidth=2)
                for enabled, value, label in (
                    (options.mean, stats.mean, "Mean"),
                    (options.median, stats.median, "Median"),
                    (options.mode, float(x[density.argmax()]), "Mode"),
                ):
                    if enabled:
                        axes.axvline(
                            value, color=color, linestyle="--", label=f"{label}: {value:.6g}"
                        )
                if options.mean and options.std:
                    mask = (x >= stats.mean - stats.std) & (x <= stats.mean + stats.std)
                    axes.fill_between(
                        x, density, where=mask, color=color, alpha=0.2, label=f"SD: {stats.std:.6g}"
                    )
            except ValueError:
                axes.axvline(stats.mean, color=color)
                self._density_note(axes)
        elif options.kind == "Box" or len(values) < 2 or stats.minimum == stats.maximum:
            axes.boxplot(
                values,
                orientation="vertical" if options.vertical else "horizontal",
                patch_artist=True,
                boxprops={"facecolor": color},
            )
            axes.set_ylabel(f"Measurement [{data.unit}]") if options.vertical else axes.set_xlabel(
                f"Measurement [{data.unit}]"
            )
            if options.kind == "Violin":
                self._density_note(axes)
        else:
            violin = axes.violinplot(
                values,
                orientation="vertical" if options.vertical else "horizontal",
                showmedians=True,
                quantiles=[0.25, 0.75] if options.quartiles else None,
            )
            for body in violin["bodies"]:
                body.set_facecolor(color)
                body.set_alpha(0.6)
            axes.set_ylabel(f"Measurement [{data.unit}]") if options.vertical else axes.set_xlabel(
                f"Measurement [{data.unit}]"
            )
        if options.grid:
            axes.grid(True, linestyle="--", linewidth=0.4, alpha=0.5)
        else:
            axes.grid(False)
        if axes.get_legend_handles_labels()[0]:
            axes.legend(loc="best")
        if options.x_limits is not None:
            axes.set_xlim(options.x_limits)
        if options.y_limits is not None:
            axes.set_ylim(options.y_limits)
        self.set_font_size(self.font_size)
        return axes

    def set_font_size(self, size: float) -> None:
        self.font_size = max(12, size)
        for axes in self.figure.axes:
            axes.title.set_fontsize(self.font_size + 2)
            for axis in (axes.xaxis, axes.yaxis):
                axis.label.set_fontsize(self.font_size)
                axis.get_offset_text().set_fontsize(self.font_size)
            axes.tick_params(labelsize=self.font_size)
            for text in axes.texts:
                text.set_fontsize(self.font_size)
            legend = axes.get_legend()
            if legend is not None:
                for text in legend.get_texts():
                    text.set_fontsize(self.font_size)

    @staticmethod
    def _density_note(axes) -> None:
        axes.text(
            0.5,
            0.95,
            "Density requires at least two distinct values.",
            transform=axes.transAxes,
            ha="center",
            va="top",
            wrap=True,
        )

    def save_png(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._data is None:
            raise ValueError("Create a plot before exporting it.")
        # Resize before Export
        export = AnalysisRenderer(Figure(figsize=(15, 8), dpi=300, layout="constrained"))
        export.render(self._data, self._options)
        with rc_context({"agg.path.chunksize": 10000}):
            export.figure.savefig(path, dpi=300, bbox_inches="tight")
        export.figure.clear()
        return path
