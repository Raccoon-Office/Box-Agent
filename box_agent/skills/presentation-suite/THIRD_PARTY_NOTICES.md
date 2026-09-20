# Third-party notices

This notice covers the third-party assets included in this bundle; it does not grant a license to SN-owned code.

Python runtime dependencies are declared in `skills/sn-ppt-standard/requirements.txt`.
Node.js exporter dependencies are declared in
`skills/sn-ppt-standard/scripts/export_pptx/package.json` and its
`skills/sn-ppt-standard/scripts/export_pptx/package-lock.json`. These dependencies
are installed separately; their license texts and metadata are available from the
corresponding upstream projects and installed environments.

The bundled sn-ppt-standard includes:

- Apache ECharts 5.5.0 static runtime at
  `skills/sn-ppt-standard/assets/vendor/echarts.min.js`, distributed under the
  Apache License 2.0. Verbatim upstream license and attribution files are retained at
  `skills/sn-ppt-standard/assets/licenses/echarts-5.5.0/LICENSE`,
  `skills/sn-ppt-standard/assets/licenses/echarts-5.5.0/NOTICE`, and
  `skills/sn-ppt-standard/assets/licenses/echarts-5.5.0/licenses/LICENSE-d3` (BSD 3-Clause subcomponent notice).
  Their upstream URLs and SHA256 hashes are recorded in `source.json`. These files
  cover the bundled static runtime, independently of the exporter npm version.
- 45 allow-listed OFL/open-source presentation fonts for offline installation,
  stored in `fonts/`. The bundled license text is retained at both
  `fonts/OFL-1.1.txt` and
  `skills/sn-ppt-standard/assets/licenses/OFL-1.1.txt`.
  The release now includes the registered Smiley Sans and IBM Plex Sans faces;
  it does not claim or reference unbundled Inter, JetBrains Mono, or Source Han
  Sans/Serif faces as delivery fonts.

The generated presentation may incorporate user-supplied or remotely retrieved material.
The deployer is responsible for ensuring that such material may lawfully be used and
distributed.
