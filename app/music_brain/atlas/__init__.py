"""Package shell for the pair atlas modules. The modules themselves are private (dj-atlas
submodule, builder/atlas); app.music_brain.atlas_bridge adds them to this package's path."""
from app.music_brain import atlas_bridge as _bridge

_bridge.extend_package(__path__)
