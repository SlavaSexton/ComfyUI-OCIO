# The thirteen nodes: what connects to what

This is the wiring map for the whole pack. It answers three questions and nothing else: what shape is each node,
what may be plugged into it, and what does it plug into. For every widget, every allowed value and every
per-node detail, read the reference for that group:

- **[NODES_IO.md](NODES_IO.md)** - `OCIO Read` and `OCIO Write`, the two nodes that touch disk.
- **[NODES_COLOR.md](NODES_COLOR.md)** - the seven colour operators plus `OCIO Player`.
- **[NODES_REPAIR.md](NODES_REPAIR.md)** - `OCIO Clip Repair`, which composites a reconstruction into a plate.
- **[NODES_VAE.md](NODES_VAE.md)** - `OCIO VAE Decode` and `OCIO VAE Encode`.

Everything below was read from the running server's `/object_info`, not from memory. If your install disagrees,
your install is the truth: ask it the same question with `GET /object_info/OCIOWrite` and friends.

## The shape of every node

An asterisk marks an optional socket. A socket with no asterisk must be connected or the prompt will not
validate. Widgets are left out here on purpose; the group references cover them.

| Node | Input sockets | Outputs |
| --- | --- | --- |
| `OCIO Read` | none, the source is a widget | `image/sequence/video:IMAGE`, `alpha:MASK`, `fps:FLOAT`, `info:STRING`, `ComfyUI Video:VIDEO`, `metadata:STRING` |
| `OCIO ColorSpace` | `image:IMAGE*`, `video:VIDEO*` | `image/sequence/video:IMAGE`, `ComfyUI Video:VIDEO` |
| `OCIO LogConvert` | `image:IMAGE*`, `video:VIDEO*` | `image/sequence/video:IMAGE`, `ComfyUI Video:VIDEO` |
| `OCIO Display` | `image:IMAGE*`, `video:VIDEO*` | `image/sequence/video:IMAGE`, `ComfyUI Video:VIDEO` |
| `OCIO CDLTransform` | `image:IMAGE*`, `video:VIDEO*` | `image/sequence/video:IMAGE`, `ComfyUI Video:VIDEO` |
| `OCIO FileTransform` | `image:IMAGE*`, `video:VIDEO*` | `image/sequence/video:IMAGE`, `ComfyUI Video:VIDEO` |
| `OCIO LookTransform` | `image:IMAGE*`, `video:VIDEO*` | `image/sequence/video:IMAGE`, `ComfyUI Video:VIDEO` |
| `OCIO Exposure` | `image:IMAGE*`, `video:VIDEO*` | `image/sequence/video:IMAGE`, `ComfyUI Video:VIDEO` |
| `OCIO Player` | `images:IMAGE*`, `video:VIDEO*`, `alpha:MASK*`, `audio:AUDIO*` | **none** |
| `OCIO VAE Encode` | `pixels:IMAGE`, `vae:VAE` | `latent:LATENT` |
| `OCIO VAE Decode` | `samples:LATENT`, `vae:VAE` | `image/sequence/video:IMAGE`, `range report:STRING` |
| `OCIO Clip Repair` | `plate:IMAGE`, `reconstruction:IMAGE` | `image:IMAGE`, `repair mask:MASK`, `report:STRING` |
| `OCIO Write` | `images:IMAGE*`, `video:VIDEO*`, `alpha:MASK*`, `audio:AUDIO*`, `source_meta:STRING*` | `path:STRING` |

Four things fall straight out of that table.

**`OCIO Read` starts a chain and `OCIO Write` ends one.** Read has no input sockets at all, because its source is
a path you type or pick. Write's only output is the path it wrote, which is a `STRING` for logging rather than a
picture. Nothing downstream needs it.

**`OCIO Player` is a dead end, and deliberately.** It has no outputs, exactly like the stock `Preview Image`. Hang
it off a wire to look at something; you cannot grade through it.

**The six colour operators are interchangeable in shape.** Every one takes an image and hands back an image, so
they chain in any order and any number. What differs is what each one does to the numbers, which is the whole of
[NODES_COLOR.md](NODES_COLOR.md).

**The two VAE nodes are the only ones that speak `LATENT`,** and they are mirror images: Encode takes pixels and
gives a latent, Decode takes a latent and gives pixels.

## Which socket accepts what

Every socket takes a standard ComfyUI type, so anything in your install that produces that type will connect. The
nodes an artist reaches for first:

| Type | Wire it from | Notes |
| --- | --- | --- |
| `IMAGE` | `LoadImage`, `VAEDecode`, `OCIO Read`, any colour operator, any generation node | The common currency. 310 node types in a typical install emit it. |
| `MASK` | `LoadImage`'s second output, `ImageToMask`, `SolidMask`, `OCIO Read` | Used as the alpha channel on Write and Player. |
| `VAE` | `VAELoader`, `CheckpointLoaderSimple` | Must be the VAE that belongs to the model. It is trained with the transformer and cannot be swapped. |
| `LATENT` | `KSampler`, `EmptyLatentImage`, `VAEEncode`, `OCIO VAE Encode` | |
| `AUDIO` | `LoadAudio`, `VAEDecodeAudio`, an audio VAE decode from a video model | `OCIO Write` muxes it into the file; `OCIO Player` plays it with the frames and meters it. |
| `VIDEO` | `LoadVideo`, `CreateVideo`, any node emitting ComfyUI's native `VIDEO` | See the rule below. |
| `STRING` | `OCIO Read`'s `metadata` output | The only intended source for Write's `metadata`. |

### The IMAGE and VIDEO sockets are mutually exclusive

Every node that has both takes one or the other, never both at once. Connect an `IMAGE` and the `VIDEO` socket
disconnects itself, and the reverse. This exists so the pack drops into ComfyUI's native video graph without a
second set of nodes: `Load Video` into a colour operator into `Save Video` works, and so does `Load Image` into
the same operator.

A `VIDEO` object carries its frame rate and its audio track along with the picture. `OCIO Write` uses both: it
takes the movie's own frame rate, and it adopts the track when nothing is wired to `audio`. The `write_audio`
widget is how you decline that, because there is no wire to disconnect.

### Metadata cannot travel on the IMAGE wire, and that is why there is a second one

An `IMAGE` in ComfyUI is a bare tensor. It has no room for a reel name, a timecode or a camera model, so no
change to this pack could make a plate's metadata survive a chain of image nodes. That is what `OCIO Read`'s sixth
output is for. Wire `metadata` straight into `OCIO Write`'s `metadata` and the identity bypasses the
picture chain completely, however many nodes are in between.

## Chains that do real work

Written as explicit wiring. Substitute your own colorspaces; these are the shapes, not a prescription.

**Look at a plate in the right colour.**

```
OCIO Read (input_colorspace = the plate's own) -> OCIO Player
```

**Grade a plate and write an EXR master, keeping the plate's identity.**

```
OCIO Read -> OCIO CDLTransform -> OCIO Write        (images)
OCIO Read: metadata     -> OCIO Write        (source_meta)
```

**Make a review movie from a scene-linear render.** The display transform goes last, immediately before the
write, because it is the end of the chain by definition. Grading after a display transform grades the wrong
numbers.

```
OCIO Read -> OCIO CDLTransform -> OCIO Display -> OCIO Write (container = video)
```

**Or in two nodes, since v1.3.0.** `OCIO Write` has a `view` widget that applies the same output transform on
the way out, and it fills itself in: choose a scene-referred `input_colorspace` and a display-referred
`output_colorspace`, and the node picks that display's view by itself. It picks the **ACES 1.3** one where
that config has it, because the version a render is made with has to match the version the comp views it
through, and a Nuke 13 or 14 project is on ACES 1.2 / 1.3. The 2.0 entries are one click away in the same
list for a 2.0 pipeline.

```
OCIO Read -> OCIO CDLTransform -> OCIO Write (container = video, view fills itself in)
```

**Do not do both.** An `OCIO Display` upstream plus a `view` on the write applies the output transform twice,
which is worse than applying it neither time. In a graph that already has the Display node, leave `view` on
`(none)`.

Which to use is taste: the separate node makes the step visible in the graph, the widget keeps the graph
shorter. The pixels are the same. What is NOT the same is leaving both off - a scene-linear render written
straight to Rec.709 carries values above 1.0 that the container then clips flat, which is the failure the
`view` widget exists to prevent. See docs/NODES_IO.md.

**Apply a show LUT.** A LUT is defined over a bounded input range, so put the data into the space the file was
authored for first. On a scene-linear plate that means a log encode ahead of it.

```
OCIO Read -> OCIO LogConvert (Linear to Log) -> OCIO FileTransform -> OCIO Write
```

**Colour-manage a generation.** The model's own decode hands you an `IMAGE`; take it from there.

```
KSampler -> VAEDecode -> OCIO ColorSpace -> OCIO Write
```

**Decode a latent without losing the range above white.** This is what the VAE nodes are for. The stock decode
clamps to 0..1 and the values are gone before any node sees them.

```
VAELoader ------------------\
KSampler -> OCIO VAE Decode -> OCIO Write
                            -> range report (STRING) into any text preview
```

**The LTX-2.5 HDR round trip.** Its VAE speaks ACEScct log codes, so the curve has to be undone before the
picture means anything. Put an `OCIO LogConvert` in the chain and do it explicitly. There is no `OCIO Write`
profile for this: the one that existed was removed, because nothing in ComfyUI puts the model on its ACEScct
path in the first place (see CHANGELOG.md).

```
OCIO Read (ACEScg) -> OCIO LogConvert (Linear to Log, ACEScct) -> OCIO VAE Encode -> [the model]
[the model] -> OCIO VAE Decode -> OCIO LogConvert (Log to Linear, ACEScct) -> OCIO Write
```

Spell the combo values exactly as the node declares them. `ACEScct` is matched by string, so `acescct` is an
HTTP 400 for the whole prompt with no fallback, and the front end will not warn you because it never sees a
hand-written API graph.

**Colour-manage a clip on the native video wire.** No image nodes anywhere in this one.

```
Load Video -> OCIO ColorSpace (video socket) -> Save Video
```

## Three ways a graph can look right and not be

**The folder is empty and the run said success.** ComfyUI's front end can send a list naming which output nodes
to run, and every output node outside that list is dropped before execution begins, silently. `OCIO Write` is an
output node. Nothing in the node can warn you, because a skipped node never executes. Queue from the graph, or
post the prompt yourself without that field. The long version is in [NODES_IO.md](NODES_IO.md).

**A preset set a colorspace you did not choose.** Every entry in `OCIO Write`'s `profile` sets both the from and
the output colorspace. That is the point of a preset, and it means the wire has to carry what the preset expects.
`SDR Rec.709 delivery` expects display-referred sRGB; hand it a scene-linear grade and you get a file tagged
Rec.709 whose values still run past 14, written without complaint.

**Something automatic did not happen.** `auto_range` walks the graph to find the upstream `OCIO Read`, which only
the canvas can do. A prompt posted straight to the API gets whatever numbers are in the JSON, with the box ticked.
Set `first_frame`, `last_frame`, `start_number` and `fps` explicitly in batch and farm submissions.
