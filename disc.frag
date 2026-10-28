#version 330

// One pass over the overlay draws, bottom to top: the record's shadow, the
// spectrum ring, the record, the tonearm and the play/pause button.
// Positions are in record radii from the record's centre, y up.

uniform vec2  u_center;     // record centre, px from the bottom-left
uniform float u_radius;     // record radius, px
uniform float u_rot;        // platter angle, radians (clockwise)
uniform sampler2D u_cover_a;
uniform sampler2D u_cover_b;
uniform float u_fade;       // 0 shows cover a, 1 shows cover b
uniform float u_label;      // 0 picture disc, 1 black vinyl with the cover as its label
uniform float u_grooves;    // groove phase per radius, set for the record's size
uniform vec3  u_dark;       // the record is drawn from u_dark (shadows)
uniform vec3  u_light;      // to u_light (highlights): black and white by default
uniform vec3  u_accent;     // spectrum ring and tonearm
uniform float u_bands[48];  // spectrum levels, low to high
uniform float u_ring;       // spectrum ring strength; 0 hides it
uniform float u_arm_show;   // 0 hides the tonearm
uniform vec2  u_pivot;      // tonearm pivot
uniform float u_arm_len;    // pivot to stylus
uniform float u_arm;        // tonearm angle
uniform float u_lift;       // 0 resting on the record, 1 lifted (paused)
uniform float u_hover;      // 0..1: the play/pause button over the centre
uniform float u_playing;    // 1 shows pause, 0 shows play
uniform float u_scale;      // the record's size with the beat pulse (1 at rest)
uniform float u_opacity;    // fades the whole overlay

out vec4 frag;

const float PI = 3.14159265;
const float GAPS[4] = float[](0.49, 0.61, 0.72, 0.84);  // silent rings between songs
const float LABEL = 0.34;
const float HOLE = 0.024;
const float LIGHT = 2.2;     // where the lamp sits, as an angle (upper left)
const float TICKS = 120.0;
const float RING = 1.06;     // where the spectrum ticks start

float px;  // one pixel, in record radii

// The spectrum wrapped around the record: bass at the bottom, treble at the
// top, mirrored left and right.
float spectrum(float th) {
    float t = abs(mod(th + 1.5 * PI, 2.0 * PI) - PI) / PI;
    float x = t * 47.0;
    int i = int(x);
    return mix(u_bands[i], u_bands[min(i + 1, 47)], fract(x));
}

mat2 rotation(float a) {
    float c = cos(a), s = sin(a);
    return mat2(c, s, -s, c);
}

float cover(vec2 q) {  // q spans -1..1 over the image
    vec2 uv = vec2(q.x, -q.y) * 0.5 + 0.5;
    return mix(texture(u_cover_a, uv).r, texture(u_cover_b, uv).r, u_fade);
}

float ring(float x, float at, float width) {
    float d = (x - at) / width;
    return exp(-d * d);
}

float segment(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    return length(pa - ba * clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0));
}

float box(vec2 p, vec2 half_size) {
    vec2 d = abs(p) - half_size;
    return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
}

float triangle(vec2 p, float r) {  // equilateral, pointing right
    p = vec2(-p.y, p.x);
    const float k = sqrt(3.0);
    p.x = abs(p.x) - r;
    p.y = p.y + r / k;
    if (p.x + k * p.y > 0.0) p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
    p.x -= clamp(p.x, -2.0 * r, 0.0);
    return -length(p) * sign(p.y);
}

// Antialiased coverage of the shape whose signed distance is d.
float fill(float d) {
    return clamp(0.5 - d / px, 0.0, 1.0);
}

// Paints a layer over the premultiplied color below.
vec4 over(vec4 below, vec3 color, float alpha) {
    return vec4(color * alpha, alpha) + below * (1.0 - alpha);
}

// Brightness of the record's surface, 0..1.
float record(vec2 p, float r, float th) {
    vec2 q = rotation(u_rot) * p;  // spins with the platter

    // Grooved area, fine grooves (faded out before they alias) and song gaps.
    float zone  = smoothstep(0.355, 0.365, r) * (1.0 - smoothstep(0.955, 0.965, r));
    float phase = r * u_grooves;
    float fine  = (0.5 + 0.5 * sin(phase)) * clamp((2.5 - fwidth(phase)) / 1.5, 0.0, 1.0);
    float gaps  = 0.0;
    for (int i = 0; i < 4; i++) gaps += ring(r, GAPS[i], 0.004);
    fine *= 1.0 - min(gaps, 1.0);

    // The lamp's reflection stays put while the record turns under it: a
    // narrow streak and a broad sheen on both sides of the spindle.
    float facing = abs(cos(th - LIGHT));
    float sheen  = pow(facing, 40.0) * 0.5 + pow(facing, 5.0) * 0.1;

    // Picture disc: the cover printed under clear vinyl.
    float picture = cover(q) * 0.88 + 0.04;
    picture *= 1.0 - zone * 0.16 * fine;
    picture += zone * sheen * (0.45 + 0.25 * fine + 0.3 * gaps);

    // Black vinyl, with the cover on the centre label.
    float vinyl = 0.045 + zone * (0.012 * fine + sheen * (0.55 + 0.45 * fine + 0.6 * gaps));
    vinyl += (1.0 - zone) * step(LABEL, r) * sheen * 0.35;  // smooth lead-in / run-out
    float onLabel = 1.0 - smoothstep(LABEL - px, LABEL + px, r);
    vinyl = mix(vinyl, cover(q / LABEL) * 0.9 + 0.03, onLabel);
    vinyl *= 1.0 - 0.6 * ring(r, LABEL, 0.003);

    float lum = mix(picture, vinyl, u_label);

    // Raised rim: darker lip with a thin lit edge.
    lum = mix(lum, lum * 0.55 + 0.03, smoothstep(0.972, 0.99, r));
    lum += 0.22 * ring(r, 0.991, 0.004) * (0.35 + facing);
    return clamp(lum, 0.0, 1.0);
}

// Distances to the tonearm's moving parts: the arm (tube and headshell) and
// the counterweight behind the pivot.
float arm(vec2 p) {
    vec2 dir    = vec2(cos(u_arm), sin(u_arm));
    vec2 stylus = u_pivot + dir * u_arm_len;
    vec2 head   = rotation(-0.4) * dir;  // the headshell angles in toward the spindle
    vec2 joint  = stylus - head * 0.15;
    float tube  = segment(p, u_pivot, joint) - max(0.011, 0.6 * px);
    float shell = segment(p, joint, stylus) - 0.028;
    return min(tube, shell);
}

float counterweight(vec2 p) {
    vec2 dir = vec2(cos(u_arm), sin(u_arm));
    return segment(p, u_pivot - dir * 0.1, u_pivot - dir * 0.19) - 0.036;
}

void main() {
    float unit = 1.0 / u_radius;                       // one pixel, in record radii
    vec2  p    = (gl_FragCoord.xy - u_center) * unit;  // the tonearm stays put...
    vec2  q    = p / u_scale;                          // ...while the record swells with the beat
    float r    = length(q);
    float th   = atan(q.y, q.x);
    px = unit / u_scale;
    float lum = record(q, r, th);  // first: it takes derivatives

    vec4 col = vec4(0.0);

    // Soft shadow under the record.
    col = over(col, vec3(0.0), 0.32 * (1.0 - smoothstep(0.92, 1.1, length(q - vec2(0.0, -0.035)))));

    // Spectrum ring: fine ticks just outside the rim, longer where the music is.
    if (u_ring > 0.0) {
        float a     = floor(th / (2.0 * PI) * TICKS + 0.5) / TICKS * 2.0 * PI;
        vec2  dir   = vec2(cos(a), sin(a));
        float level = spectrum(a);
        float len   = 0.006 + 0.11 * level * u_ring;
        float tick  = segment(q, dir * RING, dir * (RING + len)) - max(0.0025, 0.7 * px);
        col = over(col, u_accent, fill(tick) * min(1.0, u_ring * 2.5) * (0.25 + 0.55 * level));
    }

    // The record itself, with the spindle hole left open.
    float disc = fill(r - 1.0) * (1.0 - fill(r - HOLE));
    col = over(col, mix(u_dark, u_light, lum), disc);

    // Play/pause button, shown while the pointer is over the centre.
    if (u_hover > 0.0) {
        col = over(col, u_dark, 0.55 * u_hover * fill(r - 0.15));
        float pause = min(box(q - vec2(-0.028, 0.0), vec2(0.011, 0.045)),
                          box(q - vec2(0.028, 0.0), vec2(0.011, 0.045)));
        float play  = triangle(q - vec2(0.008, 0.0), 0.04);
        float glyph = mix(fill(play), fill(pause), u_playing);
        col = over(col, u_light, 0.9 * u_hover * glyph);
    }

    // Tonearm. Its shadow drifts further off when it's lifted.
    px = unit;
    if (u_arm_show > 0.0) {
        vec2  lift   = vec2(0.02, -0.03) * (1.0 + 1.5 * u_lift);
        float shadow = min(arm(p - lift), counterweight(p - lift));
        col = over(col, vec3(0.0), u_arm_show * (0.35 - 0.12 * u_lift)
                                  * (1.0 - smoothstep(-0.01, 0.035 + 0.03 * u_lift, shadow)));
        float base = length(p - u_pivot);
        col = over(col, mix(u_dark, u_light, 0.09), u_arm_show * fill(base - 0.13));
        col = over(col, u_accent, u_arm_show * 0.35 * fill(abs(base - 0.13) - 0.5 * px));
        col = over(col, mix(u_dark, u_light, 0.3), u_arm_show * fill(base - 0.05));
        col = over(col, mix(u_dark, u_light, 0.26), u_arm_show * fill(counterweight(p)));
        col = over(col, u_accent, u_arm_show * fill(arm(p)));
    }

    frag = col * u_opacity;
}
