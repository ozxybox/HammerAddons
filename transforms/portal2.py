"""Portal-2 specific transformations."""
from srctools import Output, conv_bool, conv_int, VMF

from hammeraddons.bsp_transform import trans, Context

def needs_paint(vmf: VMF) -> bool:
    """Check if we have paint."""
    for ent_cls in [
        'prop_paint_bomb',
        'paint_sphere',
        'weapon_paintgun',  # Not in retail but someone might add it.
    ]:
        if vmf.by_class[ent_cls]:
            return True

    for ent in vmf.by_class['info_paint_sprayer']:
        # Special case, this makes sprayers only render visually, which
        # works even without the value set.
        if not conv_bool(ent['DrawOnly']):
            return True

    for ent_cls in [
        'prop_weighted_cube',
        'prop_physics_paintable',
    ]:
        for ent in vmf.by_class[ent_cls]:
            # If the cube is bouncy, enable paint.
            if conv_int(ent['paintpower', '4'], 4) != 4:
                return True
    return False


@trans('Force Paint in Map')
def force_paintinmap(ctx: Context):
    """If paint entities are present, set paint in map to true."""
    # Already set, don't bother confirming.
    if conv_bool(ctx.vmf.spawn['paintinmap']):
        return

    if needs_paint(ctx.vmf):
        ctx.vmf.spawn['paintinmap'] = '1'
        # Ensure we have some blobs.
        if conv_int(ctx.vmf.spawn['maxblobcount']) == 0:
            ctx.vmf.spawn['maxblobcount'] = '250'

