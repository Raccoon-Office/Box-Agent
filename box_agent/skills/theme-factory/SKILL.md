---
name: theme-factory
description: Toolkit for styling non-deck artifacts with one of 10 legacy color/font themes. PPT, PPTX, slide, and deck theme discovery is owned by the pptx skill and must be redirected there.
keywords: [theme, palette, color, font, style, 主题, 配色, 风格, 字体, 样式, 视觉风格]
related_skills: [pptx]
license: Complete terms in LICENSE.txt
---


# Theme Factory Skill

This skill provides a curated collection of professional font and color themes themes, each with carefully selected color palettes and font pairings. Once a theme is chosen, it can be applied to any artifact.

## PPT ownership boundary (mandatory)

For any request about a PPT, PPTX, presentation, slide, or deck—including
“有什么主题”, “先看主题”, “让我选风格”, or applying a theme to a deck—this
legacy 10-palette catalog is **not** authoritative. Immediately load
`pptx` with `get_skill(skill_name="pptx")` and follow the selected mode's theme
discovery route. In quick mode, `ppt-fast` owns the controlled theme gallery.
Do not list the ten themes below, do not show
`theme-showcase.pdf`, and do not map their names into a controlled deck. The
`ppt-fast` skill's bundled `themes/*.json` ids define its executable themes;
creative mode follows its own selected outlet's design guidance.

Continue with this skill only for a non-deck artifact, or when the user
explicitly asks to reuse one of these legacy palettes on such an artifact.

## Purpose (non-deck artifacts)

To apply consistent, professional styling to non-deck artifacts, use this skill. Each theme includes:
- A cohesive color palette with hex codes
- Complementary font pairings for headers and body text
- A distinct visual identity suitable for different contexts and audiences

## Usage Instructions

To apply styling to a slide deck or other artifact:

1. **Show the theme showcase**: Display the `theme-showcase.pdf` file to allow users to see all available themes visually. Do not make any modifications to it; simply show the file for viewing.
2. **Ask for their choice**: Ask which theme to apply to the deck
3. **Wait for selection**: Get explicit confirmation about the chosen theme
4. **Apply the theme**: Once a theme has been chosen, apply the selected theme's colors and fonts to the deck/artifact

## Themes Available

The following 10 themes are available, each showcased in `theme-showcase.pdf`:

1. **Ocean Depths** - Professional and calming maritime theme
2. **Sunset Boulevard** - Warm and vibrant sunset colors
3. **Forest Canopy** - Natural and grounded earth tones
4. **Modern Minimalist** - Clean and contemporary grayscale
5. **Golden Hour** - Rich and warm autumnal palette
6. **Arctic Frost** - Cool and crisp winter-inspired theme
7. **Desert Rose** - Soft and sophisticated dusty tones
8. **Tech Innovation** - Bold and modern tech aesthetic
9. **Botanical Garden** - Fresh and organic garden colors
10. **Midnight Galaxy** - Dramatic and cosmic deep tones

## Theme Details

Each theme is defined in the `themes/` directory with complete specifications including:
- Cohesive color palette with hex codes
- Complementary font pairings for headers and body text
- Distinct visual identity suitable for different contexts and audiences

## Application Process

After a preferred theme is selected:
1. Read the corresponding theme file from the `themes/` directory
2. Apply the specified colors and fonts consistently throughout the deck
3. Ensure proper contrast and readability
4. Maintain the theme's visual identity across all slides

## Create your Own Theme
To handle cases where none of the existing themes work for an artifact, create a custom theme. Based on provided inputs, generate a new theme similar to the ones above. Give the theme a similar name describing what the font/color combinations represent. Use any basic description provided to choose appropriate colors/fonts. After generating the theme, show it for review and verification. Following that, apply the theme as described above.
