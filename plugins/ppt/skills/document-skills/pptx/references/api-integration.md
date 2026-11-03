# API Integration Notes

## 1. Skill shape

1. This directory is a skill package, not a Responses tool definition.
2. If exposed via API, wrap deck behavior into narrow file-based tool calls.

## 2. Suggested tool names

1. `inspect_pptx`
1. `render_pptx`
1. `create_pptx_from_spec`
1. `edit_pptx_from_plan`

## 3. Common parameters

1. `input_path`
1. `output_path`
1. `slide_range`
1. `plan_path`
1. `render_dir`
1. `validation_mode`

## 3. Data visibility constraint

1. Do not assume direct model visibility of `.pptx` content.
2. Always convert to rendered images or extracted text for model reasoning.
