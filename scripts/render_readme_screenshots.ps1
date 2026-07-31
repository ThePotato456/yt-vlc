Add-Type -AssemblyName System.Drawing

$outputDirectory = Join-Path $PSScriptRoot '..\docs\screenshots'
$font = [System.Drawing.Font]::new(
    'Consolas',
    18,
    [System.Drawing.FontStyle]::Regular,
    [System.Drawing.GraphicsUnit]::Pixel
)
$smallFont = [System.Drawing.Font]::new(
    'Segoe UI',
    14,
    [System.Drawing.FontStyle]::Regular,
    [System.Drawing.GraphicsUnit]::Pixel
)

$colors = @{
    Background = [System.Drawing.ColorTranslator]::FromHtml('#0d1117')
    TitleBar   = [System.Drawing.ColorTranslator]::FromHtml('#161b22')
    Border     = [System.Drawing.ColorTranslator]::FromHtml('#30363d')
    Text       = [System.Drawing.ColorTranslator]::FromHtml('#c9d1d9')
    Muted      = [System.Drawing.ColorTranslator]::FromHtml('#8b949e')
    Cyan       = [System.Drawing.ColorTranslator]::FromHtml('#39c5cf')
    Green      = [System.Drawing.ColorTranslator]::FromHtml('#3fb950')
    Blue       = [System.Drawing.ColorTranslator]::FromHtml('#58a6ff')
    Yellow     = [System.Drawing.ColorTranslator]::FromHtml('#d29922')
    Magenta    = [System.Drawing.ColorTranslator]::FromHtml('#d2a8ff')
}

$banner = @(
    ' __   _______       __     ___     ____'
    ' \ \ / /_   _|      \ \   / / |   / ___|'
    '  \ V /  | |  _____  \ \ / /| |  | |'
    '   | |   | | |_____|  \ V / | |__| |___'
    '   |_|   |_|           \_/  |_____\____|'
)

function New-TerminalScreenshot {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [int] $Height,

        [Parameter(Mandatory)]
        [string] $Title,

        [Parameter(Mandatory)]
        [array] $Lines
    )

    $bitmap = [System.Drawing.Bitmap]::new(1200, $Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    $graphics.Clear($colors.Background)

    $titleBrush = [System.Drawing.SolidBrush]::new($colors.TitleBar)
    $graphics.FillRectangle($titleBrush, 0, 0, 1200, 48)
    $borderPen = [System.Drawing.Pen]::new($colors.Border, 2)
    $graphics.DrawRectangle($borderPen, 1, 1, 1197, $Height - 3)

    $graphics.FillEllipse([System.Drawing.SolidBrush]::new(
        [System.Drawing.ColorTranslator]::FromHtml('#ff5f56')
    ), 20, 18, 14, 14)
    $graphics.FillEllipse([System.Drawing.SolidBrush]::new(
        [System.Drawing.ColorTranslator]::FromHtml('#ffbd2e')
    ), 43, 18, 14, 14)
    $graphics.FillEllipse([System.Drawing.SolidBrush]::new(
        [System.Drawing.ColorTranslator]::FromHtml('#27c93f')
    ), 66, 18, 14, 14)

    $mutedBrush = [System.Drawing.SolidBrush]::new($colors.Muted)
    $titleSize = $graphics.MeasureString($Title, $smallFont)
    $graphics.DrawString(
        $Title,
        $smallFont,
        $mutedBrush,
        (1200 - $titleSize.Width) / 2,
        16
    )

    $brushes = @{}
    foreach ($name in $colors.Keys) {
        $brushes[$name] = [System.Drawing.SolidBrush]::new($colors[$name])
    }

    $y = 70
    foreach ($line in $banner) {
        $graphics.DrawString($line, $font, $brushes.Cyan, 42, $y)
        $y += 27
    }
    $graphics.DrawString(
        '       direct network playback via yt-dlp + VLC',
        $font,
        $brushes.Muted,
        42,
        $y + 5
    )
    $y += 58

    foreach ($line in $Lines) {
        $graphics.DrawString($line.Text, $font, $brushes[$line.Color], 42, $y)
        $y += if ($line.Spacing) { $line.Spacing } else { 32 }
    }

    $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)

    foreach ($brush in $brushes.Values) {
        $brush.Dispose()
    }
    $mutedBrush.Dispose()
    $titleBrush.Dispose()
    $borderPen.Dispose()
    $graphics.Dispose()
    $bitmap.Dispose()
}

$playbackLines = @(
    @{ Text = '[MEDIA] Man''s $1500 Apple Gift Card Scheme Explodes In His Face.'; Color = 'Magenta'; Spacing = 38 }
    @{ Text = '        creator  Multi-State'; Color = 'Text' }
    @{ Text = '        duration 29:25'; Color = 'Text' }
    @{ Text = '        quality  1920x1080 @ 25 fps'; Color = 'Text' }
    @{ Text = '        format   399+251 - video + audio'; Color = 'Text' }
    @{ Text = '        codecs   av01.0.08M.08 + opus'; Color = 'Text' }
    @{ Text = '        size     ~313.5 MiB'; Color = 'Text'; Spacing = 48 }
    @{ Text = '[OK] Stream handed off to VLC - enjoy'; Color = 'Green' }
)

$setupLines = @(
    @{ Text = '[GET] Downloading yt-dlp.exe'; Color = 'Blue'; Spacing = 38 }
    @{ Text = '  yt-dlp.exe   [################################] 100.0%   17.4 MiB / 17.4 MiB'; Color = 'Cyan'; Spacing = 38 }
    @{ Text = '[OK] Downloaded yt-dlp.exe (17.4 MiB)'; Color = 'Green'; Spacing = 46 }
    @{ Text = '[GET] Downloading vlc.zip'; Color = 'Blue'; Spacing = 38 }
    @{ Text = '  vlc.zip      [################################] 100.0%   76.2 MiB / 76.2 MiB'; Color = 'Cyan'; Spacing = 38 }
    @{ Text = '[OK] Downloaded vlc.zip (76.2 MiB)'; Color = 'Green'; Spacing = 38 }
    @{ Text = '[...] Extracting portable VLC'; Color = 'Yellow'; Spacing = 38 }
    @{ Text = '[OK] Portable VLC extracted'; Color = 'Green' }
)

New-TerminalScreenshot `
    -Path (Join-Path $outputDirectory 'playback.png') `
    -Height 610 `
    -Title 'PowerShell - yt-vlc' `
    -Lines $playbackLines

New-TerminalScreenshot `
    -Path (Join-Path $outputDirectory 'first-run-setup.png') `
    -Height 650 `
    -Title 'PowerShell - first run' `
    -Lines $setupLines

$font.Dispose()
