using Microsoft.UI.Xaml.Controls;
using Windows.Foundation;

namespace Xass.Native.Controls;

// Wrap each button using its own measured width. Uniform cells sized from the
// shortest label truncate longer translated actions.
public sealed class FlowPanel : Panel
{
    public double HorizontalSpacing { get; set; } = 8;
    public double VerticalSpacing { get; set; } = 8;

    protected override Size MeasureOverride(Size availableSize)
    {
        double limit = Math.Max(0, availableSize.Width), x = 0, y = 0, row = 0, width = 0;
        foreach (var child in Children)
        {
            child.Measure(new Size(limit, double.PositiveInfinity));
            var size = child.DesiredSize;
            if (x > 0 && x + size.Width > limit)
            {
                width = Math.Max(width, x - HorizontalSpacing);
                x = 0; y += row + VerticalSpacing; row = 0;
            }
            x += size.Width + HorizontalSpacing;
            row = Math.Max(row, size.Height);
        }
        return new Size(Math.Max(width, Math.Max(0, x - HorizontalSpacing)), y + row);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        double limit = Math.Max(0, finalSize.Width), x = 0, y = 0, row = 0;
        foreach (var child in Children)
        {
            var size = child.DesiredSize;
            double width = Math.Min(size.Width, limit);
            if (x > 0 && x + width > limit) { x = 0; y += row + VerticalSpacing; row = 0; }
            child.Arrange(new Rect(x, y, width, size.Height));
            x += width + HorizontalSpacing;
            row = Math.Max(row, size.Height);
        }
        return finalSize;
    }
}
