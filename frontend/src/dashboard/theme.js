// AG Studio theme matched to Procuro's own tokens in src/styles.css, so the dashboard reads as part
// of the app: slate paper canvas, white widget sheets, navy ink, and the money colors
// (held = highlighter yellow, paid = ledger green, returned = blue, problem = brick).

import { studioTheme } from "ag-studio";

const v = (name) => `var(--${name})`;
const line = (color) => ({ width: 1.5, color: v(color) });

// Chart series in a fixed order so money meanings stay consistent across charts.
const SERIES = ["#15253a", "#22785a", "#d9a800", "#5f8fc4", "#b8432f", "#7a8796", "#3c5a80", "#8fb39f", "#c98c3a", "#9aa7b6"];
const palette = Object.fromEntries(
  SERIES.flatMap((c, i) => [
    [`chartPaletteFills${i + 1}Color`, c],
    [`chartPaletteStrokes${i + 1}Color`, c],
  ])
);

export const mandateStudioTheme = studioTheme.withParams({
  fontFamily: v("body"),
  fontSize: 14,
  textColor: v("ink"),
  foregroundColor: v("ink"),
  subtleTextColor: v("ink-soft"),
  backgroundColor: v("sheet"),
  borderColor: v("line"),
  accentColor: v("ink"),
  iconColor: v("ink"),
  rowHoverColor: "#f1f4f8",

  studioWrapperBorder: false,
  studioWrapperBorderRadius: 0,
  studioWrapperSpacing: 0,
  studioWrapperBackgroundColor: v("paper"),
  studioCanvasBorder: false,
  studioCanvasBorderRadius: 0,
  studioCanvasShadow: false,
  studioCanvasBackgroundColor: v("paper"),
  studioCanvasPadding: 12,
  studioCanvasRowHeight: 18,
  studioCanvasFontFamily: v("body"),

  studioWidgetBackgroundColor: v("sheet"),
  studioWidgetBorder: line("line"),
  studioWidgetBorderRadius: 8,
  studioWidgetPadding: 12,
  studioWidgetTitleFontFamily: v("display"),
  studioWidgetTitleFontSize: 16,
  studioWidgetTitleFontWeight: 700,
  studioWidgetTitleTextColor: v("ink"),
  studioWidgetSubtitleFontFamily: v("body"),
  studioWidgetSubtitleFontSize: 13,
  studioWidgetSubtitleTextColor: v("ink-soft"),
  studioWidgetCaptionFontFamily: v("body"),
  studioWidgetCaptionFontSize: 13,
  studioWidgetCaptionTextColor: v("ink-soft"),
  studioWidgetResizeBorderColor: v("ink"),

  studioPanelContainerBackgroundColor: v("sheet"),
  studioPanelContainerBorder: line("line"),
  studioPanelContainerBorderRadius: 0,
  studioPanelDividerColor: v("line"),
  studioPanelDividerActiveColor: v("ink"),
  studioPanelSectionBorderColor: v("line"),
  studioPanelHeaderFontFamily: v("display"),
  studioPanelHeaderFontWeight: 700,
  studioToggleButtonActiveBackgroundColor: "#fff8dc",
  studioToggleButtonActiveBorderColor: v("ink"),
  studioToggleButtonActiveColor: v("ink"),

  gridFontFamily: v("body"),
  gridFontSize: 14,
  gridCellTextColor: v("ink"),
  gridHeaderBackgroundColor: v("sheet"),
  gridHeaderTextColor: v("ink-soft"),
  gridHeaderFontWeight: 700,
  gridHeaderFontSize: 13,
  gridRowHeight: 38,
  gridRowBorder: { width: 1, color: "#e3e8ee" },
  gridHeaderRowBorder: line("line"),
  gridPinnedRowBorder: line("line"),
  gridRowHoverColor: "#f1f4f8",
  gridWrapperBorder: false,

  chartFontFamily: v("body"),
  chartTextColor: v("ink"),
  chartSubtleTextColor: v("ink-soft"),
  chartAxisLineColor: v("line"),
  chartGridLineColor: "#e3e8ee",
  ...palette,
});