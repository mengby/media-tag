"""Exact output/recipe review only. No inherited approval or raw-original fallback."""

def reviewed(connection, variant):
    return review_state(connection,variant)=='approved'


def review_state(connection, variant):
    if variant['privacy_status']=='not_applicable': return 'not_applicable'
    row=connection.execute('SELECT * FROM privacy_reviews WHERE asset_id=? ORDER BY review_id DESC LIMIT 1', (variant['asset_id'],)).fetchone()
    if row and row['output_sha256']==variant['output_sha256'] and row['recipe_hash']==variant['recipe_hash']:
        return row['decision']
    return 'pending'
