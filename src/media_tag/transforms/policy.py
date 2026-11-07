"""Exact output/recipe review only. No inherited approval or raw-original fallback."""

def reviewed(connection, variant):
    return review_state(connection,variant)=='approved'


def review_state(connection, variant):
    if variant['privacy_status']=='not_applicable': return 'not_applicable'
    row=connection.execute('SELECT * FROM privacy_reviews WHERE asset_id=? ORDER BY review_id DESC LIMIT 1', (variant['asset_id'],)).fetchone()
    if row and row['output_sha256']==variant['output_sha256'] and row['recipe_hash']==variant['recipe_hash']:
        return row['decision']
    return 'pending'


def has_privacy_lineage(connection, asset):
    seen=set()
    while asset['kind']=='derived':
        if asset['asset_id'] in seen: raise ValueError('cyclic variant lineage')
        seen.add(asset['asset_id'])
        variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(asset['asset_id'],)).fetchone()
        if variant is None: raise ValueError('variant lineage unavailable')
        if variant['privacy_status']=='pending': return True
        asset=connection.execute('SELECT * FROM assets WHERE asset_id=? AND deleted_at IS NULL',(asset['parent_asset_id'],)).fetchone()
        if asset is None: raise ValueError('source lineage unavailable')
    return False
