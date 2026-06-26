Drop your logo files here, then point the env vars in ../.env at them:

  construction_logo.png   ->  CONSTRUCTION_LOGO=/app/assets/construction_logo.png
  property_logo.png       ->  PROPERTY_LOGO=/app/assets/property_logo.png

PNG with a transparent background works best (SVG: export to PNG first).
The renderer scales each logo to fit its slot automatically.
Missing files fall back to a dashed placeholder box, so the stream still runs.

Custom brand fonts: drop .ttf files here too and point FONT_BOLD / FONT_REG
in ../.env at e.g. /app/assets/YourFont-Bold.ttf
