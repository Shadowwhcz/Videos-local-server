# Video Media Library Electric Cyber Design

## Summary

This project will be refocused into a unified local video media library. The current mixed state, where the homepage presents collaboration documents while the player still behaves like a video product, will be resolved by restoring a video-first information architecture and applying a shared `electric_cyber` visual language from the bundled `stitch/stitch` templates.

The target outcome is a coherent two-page experience:

- A cinematic video library homepage based on `stitch/stitch/media_library_home_electric_cyber`
- An immersive playback page based on `stitch/stitch/immersive_video_player_electric_cyber`

Existing backend video capabilities remain in place. Collaboration routes remain in code for now, but are removed from the primary user journey.

## Goals

- Restore the homepage to a video media library instead of a collaboration dashboard
- Apply one consistent visual system across library and playback pages
- Preserve working video capabilities already present in the backend
- Reduce product confusion by removing collaboration UI from the main flow
- Keep the implementation incremental and low-risk by reusing current APIs

## Non-Goals

- Fully deleting collaboration features and routes in this iteration
- Rebuilding backend video indexing, streaming, or integrity-checking logic
- Introducing a SPA frontend or replacing FastAPI/Jinja
- Adding new server-side persistence beyond what already exists

## Product Direction

### Primary Product Shape

The application becomes a private, authenticated, local-network media library for browsing and watching local video files. The product should feel closer to a premium self-hosted streaming surface than a file manager.

### Navigation Model

- `/` is the primary library homepage
- `/play/{video_id}` is the primary playback route
- Collaboration routes remain reachable only by direct URL during this iteration
- The main navigation no longer advertises collaboration features

## User Experience

### Homepage

The homepage follows a cinematic media-library composition:

- Floating top navigation with brand, search, directory selection, refresh, and logout
- High-impact hero/recommended module using neon-glass layering and asymmetrical composition
- Video library grid with visually rich cards
- Secondary metadata surfaces for directory context, library counts, or recent content

The page should communicate "browse and watch" immediately. It should not resemble a generic CRUD dashboard or file listing.

### Playback Page

The playback page follows a cinematic split layout:

- Large primary player canvas on the left
- Supporting sidebar on the right for metadata and next-up content
- Overlay title and summary on top of the video area
- Existing playback memory and keyboard shortcuts preserved
- Mobile layout collapses into a video-first stacked flow

The player should make the video the protagonist while keeping supporting information close at hand.

## Visual System

### Source of Truth

The visual direction is derived from:

- `stitch/stitch/electric_cyber/DESIGN.md`
- `stitch/stitch/media_library_home_electric_cyber/code.html`
- `stitch/stitch/immersive_video_player_electric_cyber/code.html`

### Design Principles

- Deep navy and charcoal foundations, not pure black
- Electric cyan as the main active color
- Secondary violet and sparing hot accents for energy and hierarchy
- Frosted-glass panels, tonal layering, gradients, and glow instead of hard lines
- Intentional asymmetry to avoid a templated Bootstrap look

### Rules

- Avoid 1px solid section dividers where possible
- Use surface shifts, blur, and spacing to separate zones
- Keep corners moderately rounded rather than bubbly
- Maintain one shared token set across homepage and player

## Information Architecture

### Homepage Sections

1. Top navigation
2. Hero/recommended area
3. Directory and search context
4. Video library grid
5. Optional secondary library insights

### Playback Sections

1. Return/library affordance
2. Main video stage
3. Overlay metadata
4. Right sidebar for details and next-up list
5. Mobile-first fallback stacking

## Data and Backend Reuse

### Existing Backend To Reuse

- Video scanning and indexing
- Directory discovery
- Search filtering
- Range-based streaming
- Thumbnail generation endpoints
- Video info endpoints
- Integrity and availability checks
- Auth/session flow

### Data Flow

#### Homepage

- Homepage route should render real video data instead of collaboration data
- Template receives:
  - available directories
  - selected directory, if any
  - search term, if any
  - list of videos for the current view
  - current user/auth state
  - lightweight hero/recommended candidates derived from current video data

#### Playback

- Playback route keeps using existing `video_id` lookup and stream endpoint
- Template receives:
  - current video metadata
  - lightweight related or next-up videos from the same directory or recent scan results
  - current user/auth state where needed

## Existing Features To Preserve

- Search
- Directory filtering
- Thumbnail lazy loading
- Playback progress memory via local storage
- Keyboard shortcuts
- Fullscreen support
- Downloading/corrupted-state detection
- Login/logout flow

These features should survive the redesign unless an implementation constraint forces a visible downgrade, in which case that downgrade must be called out explicitly during implementation.

## Collaboration Feature Handling

### This Iteration

- Collaboration UI is removed from the homepage and main navigation
- Collaboration routes and backend store remain in code
- No destructive removal of collaboration code in this pass

### Reasoning

This avoids broad backend churn while re-establishing a clear product identity. It also keeps future options open if collaboration functionality needs to be extracted or retired later.

## Frontend Implementation Shape

### Templates

- `templates/index.html` becomes the new media library homepage
- `templates/play.html` becomes the new immersive playback page

### Styles

- `static/style.css` is updated to define shared electric-cyber tokens and page layouts
- Existing Bootstrap-era styles that conflict with the new direction should be minimized or retired where safe

### Scripts

- `static/app.js` keeps useful video behaviors
- Homepage logic should align with real homepage DOM again
- Dead or mismatched collaboration-homepage assumptions should be removed or bypassed

## Error Handling and Edge Cases

- Empty library state should still render a strong branded empty state
- Missing thumbnails should degrade to intentional poster placeholders
- Corrupted or downloading videos should be visibly labeled and non-playable
- Missing directory filters should gracefully fall back to all videos
- Playback page should still handle missing or invalid `video_id` with current backend error behavior

## Accessibility and Responsiveness

- Maintain strong contrast for primary text against dark surfaces
- Ensure interactive controls remain usable on touch devices
- Preserve keyboard usability for the player
- Avoid hover-only critical actions on mobile
- Keep layout functional from phone widths through large desktop displays

## Risks

### Product Risks

- The project currently mixes two product identities; partial cleanup could leave confusing remnants

### Technical Risks

- Homepage route context must be changed back from collaboration data to video data
- Existing `static/app.js` assumes old homepage/player selectors in places
- Existing working tree already contains unrelated modifications, so implementation must avoid overwriting them blindly

## Implementation Strategy

1. Rewire homepage route and template back to a video-first experience
2. Rebuild homepage UI using the `media_library_home_electric_cyber` direction
3. Rebuild playback page using the `immersive_video_player_electric_cyber` direction
4. Reconnect preserved behaviors in `static/app.js`
5. Clean up conflicting styles and stale homepage assumptions
6. Verify auth, browsing, and playback still work end-to-end

## Testing Strategy

- Route-level smoke check for `/`, `/play/{video_id}`, login, and logout
- Manual verification of directory filter and search
- Manual verification of video playback and progress restore
- Manual verification of downloading/corrupted-state handling on library cards
- Responsive checks for homepage and playback page

## Open Follow-Up After This Iteration

- Decide whether collaboration features should be removed, extracted, or moved behind a separate route namespace and dedicated templates
- Decide whether the homepage should support explicit curated shelves such as "Recently Added" or "Continue Watching"
- Consider splitting monolithic frontend styles into page-scoped sections once the redesign is stable
