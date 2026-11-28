# NW-13 hotel-room/corridor production

`hotel_corridor.production.json` is the cross-document production manifest. It
binds:

* `hotel_corridor.nwgraph` - backend-neutral editable graph using the shared
  typed operators;
* `geometry_room.json` - bounded solid geometry and CSG recipes;
* `hotel_corridor.scene.json` - stable objects, materials, local lights,
  hierarchy, typed tracks, camera shots, cut and one-shot cues;
* `hotel_corridor.song.json` - versioned instrument/sequence payload;
* the particle emitter and declared render tolerances.

`geometry_operators.nwgraph` is the focused public-schema fixture covering all
sixteen bounded geometry operators. `evidence/` retains the actual fixed-tick
frames and contact sheet. All content is original; no Farbrausch code or asset
is used.

