/** Decorative 3D scene: five slices of an app, lit one after another (the migration happens slice by slice). */
export function HeroScene() {
  return (
    <div className="scene" aria-hidden="true">
      <div className="stack">
        {[0, 1, 2, 3, 4].map((i) => <div key={i} className="plate" style={{ ["--i" as string]: i }} />)}
        <span className="chip3d" style={{ ["--i" as string]: 0, left: "6%", top: "58%" }}>legacy</span>
        <span className="chip3d" style={{ ["--i" as string]: 2, right: "4%", top: "24%" }}>parity ✓</span>
      </div>
    </div>
  );
}
