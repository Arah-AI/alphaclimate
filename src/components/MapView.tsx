"use client";

import * as React from "react";
import {
  Map as MapLibreMap,
  Marker,
  NavigationControl,
  Popup,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Card, CardHead, Select, Skeleton, ErrorNote } from "./ui";
import {
  getLegend,
  getMapAssets,
  getTileLayers,
  type Legend,
  type MapAsset,
  type TileLayerInfo,
} from "@/lib/api";
import type { Summary } from "@/lib/types";
import { money, pct, BAND_COLOR, BAND_LABEL } from "@/lib/format";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

const HAZARD_SOURCE = "hazard";
const HAZARD_LAYER = "hazard-raster";

/** CARTO's token-free raster basemap, desaturated so the data sits on top. */
const BASEMAP = {
  version: 8 as const,
  sources: {
    carto: {
      type: "raster" as const,
      tiles: [
        "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
        "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
        "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
      ],
      tileSize: 256,
      maxzoom: 19,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · © <a href="https://carto.com/attributions">CARTO</a>',
    },
  },
  layers: [
    { id: "bg", type: "background" as const, paint: { "background-color": "#f4f6f8" } },
    {
      id: "carto",
      type: "raster" as const,
      source: "carto",
      paint: { "raster-opacity": 0.72, "raster-saturation": -0.75 },
    },
  ],
};

function reducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

export function MapView({
  data,
  onOpenAsset,
}: {
  data: Summary;
  onOpenAsset: (id: string) => void;
}) {
  const holder = React.useRef<HTMLDivElement>(null);
  const map = React.useRef<MapLibreMap | null>(null);
  const [ready, setReady] = React.useState(false);

  const [layers, setLayers] = React.useState<TileLayerInfo[]>([]);
  // Water stress opens the view because it is global, dense and near-native at
  // the zoom the portfolio fits into. The flood layers are one click away and
  // are the ones that price, but they are thin coastlines at world zoom.
  const [hazard, setHazard] = React.useState("water_risk");
  const [legend, setLegend] = React.useState<Legend | null>(null);
  const [coords, setCoords] = React.useState<MapAsset[]>([]);
  const [opacity, setOpacity] = React.useState(0.75);
  const [error, setError] = React.useState<string | null>(null);

  const currency = data.portfolio.currency;

  /* ------------------------------------------------------------- metadata */
  React.useEffect(() => {
    let live = true;
    Promise.all([getTileLayers(), getMapAssets()])
      .then(([l, a]) => {
        if (!live) return;
        setLayers(l.layers);
        setCoords(a.assets);
      })
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, []);

  React.useEffect(() => {
    let live = true;
    getLegend(hazard)
      .then((l) => live && setLegend(l))
      .catch(() => live && setLegend(null));
    return () => {
      live = false;
    };
  }, [hazard]);

  /* ------------------------------------------------------------ map setup */
  React.useEffect(() => {
    if (!holder.current || map.current) return;
    const m = new MapLibreMap({
      container: holder.current,
      style: BASEMAP,
      center: [80, 12],
      zoom: 1.4,
      minZoom: 0.8,
      maxZoom: 12,
      attributionControl: { compact: true },
    });
    m.addControl(new NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => setReady(true));
    map.current = m;
    return () => {
      m.remove();
      map.current = null;
      setReady(false);
    };
  }, []);

  /* ------------------------------------------------------- hazard raster */
  const maxZoom = layers.find((l) => l.id === hazard)?.max_zoom;
  React.useEffect(() => {
    const m = map.current;
    if (!m || !ready || maxZoom === undefined) return;
    if (m.getLayer(HAZARD_LAYER)) m.removeLayer(HAZARD_LAYER);
    if (m.getSource(HAZARD_SOURCE)) m.removeSource(HAZARD_SOURCE);
    m.addSource(HAZARD_SOURCE, {
      type: "raster",
      // The pyramid stops here; maplibre overzooms the top level rather than
      // asking for tiles the store does not have.
      tiles: [`${API_BASE}/api/tiles/${hazard}/{z}/{x}/{y}.png`],
      tileSize: 512,
      minzoom: 0,
      maxzoom: maxZoom,
      attribution: "OS-Climate hazard indicators",
    });
    m.addLayer({
      id: HAZARD_LAYER,
      type: "raster",
      source: HAZARD_SOURCE,
      paint: { "raster-opacity": opacity, "raster-fade-duration": 0 },
    });
    // opacity is applied on add and then kept in step by the effect below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hazard, maxZoom, ready]);

  React.useEffect(() => {
    const m = map.current;
    if (m?.getLayer(HAZARD_LAYER)) {
      m.setPaintProperty(HAZARD_LAYER, "raster-opacity", opacity);
    }
  }, [opacity]);

  /* ------------------------------------------------------------- markers */
  const rows = React.useMemo(() => {
    const where = new Map(coords.map((c) => [c.id, c]));
    return data.assets
      .map((a) => ({ ...a, at: where.get(a.id) }))
      .filter((a): a is typeof a & { at: MapAsset } => Boolean(a.at));
  }, [data.assets, coords]);

  React.useEffect(() => {
    const m = map.current;
    if (!m || !ready || !rows.length) return;

    const biggest = Math.max(...rows.map((r) => r.eal), 1);
    const popup = new Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 14,
      className: "ac-map-popup",
    });
    const markers = rows.map((a) => {
      const r = 8 + 14 * Math.sqrt(a.eal / biggest);
      const el = document.createElement("button");
      el.type = "button";
      el.className = "ac-map-pin";
      el.setAttribute(
        "aria-label",
        `${a.name}, ${a.country}. ${money(a.eal, currency)} expected annual loss, ${BAND_LABEL[a.band]} risk. Open asset detail.`,
      );
      el.style.width = `${r * 2}px`;
      el.style.height = `${r * 2}px`;
      el.style.background = BAND_COLOR[a.band];

      const show = () => {
        popup
          .setLngLat([a.at.lon, a.at.lat])
          .setHTML(
            `<b>${escapeHtml(a.name)}</b><span>${escapeHtml(a.country)}</span>` +
              `<span><b>${money(a.eal, currency)}</b> expected annual loss</span>` +
              `<span>${pct(a.eal_pct, 2)} of value · ${BAND_LABEL[a.band]} risk</span>`,
          )
          .addTo(m);
      };
      const hide = () => popup.remove();
      el.addEventListener("mouseenter", show);
      el.addEventListener("focus", show);
      el.addEventListener("mouseleave", hide);
      el.addEventListener("blur", hide);
      el.addEventListener("click", () => onOpenAsset(a.id));

      return new Marker({ element: el }).setLngLat([a.at.lon, a.at.lat]).addTo(m);
    });

    const lons = rows.map((r) => r.at.lon);
    const lats = rows.map((r) => r.at.lat);
    m.fitBounds(
      [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ],
      { padding: 70, maxZoom: 4, duration: reducedMotion() ? 0 : 900 },
    );

    return () => {
      popup.remove();
      markers.forEach((mk) => mk.remove());
    };
  }, [rows, ready, currency, onOpenAsset]);

  if (error) return <ErrorNote message={error} />;

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_336px] items-start">
      <Card className="min-w-0">
        <CardHead title="Hazard map" stack>
          <Select
            value={hazard}
            onChange={setHazard}
            label="Hazard layer"
            options={layers.map((l) => ({ value: l.id, label: l.label }))}
          />
        </CardHead>

        <div
          ref={holder}
          role="region"
          aria-label="Portfolio assets on the hazard raster. Every asset is also listed, with the same figures, in the assets view."
          className="ac-map h-[520px] w-full rounded-[16px] overflow-hidden border border-line bg-canvas"
        />

        <div className="flex flex-wrap items-center gap-3 mt-4">
          <label
            htmlFor="ac-hazard-opacity"
            className="text-[12.5px] text-muted shrink-0"
          >
            Layer opacity
          </label>
          <input
            id="ac-hazard-opacity"
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={opacity}
            onChange={(e) => setOpacity(Number(e.target.value))}
            aria-label="Hazard layer opacity"
            aria-valuetext={pct(opacity, 0)}
            className="w-[180px] accent-[#0b6be1]"
          />
          <span className="text-[12.5px] text-muted tabular-nums w-[42px]">
            {pct(opacity, 0)}
          </span>
          <span className="text-[12px] text-muted ml-auto">
            {rows.length} of {data.assets.length} assets placed
          </span>
        </div>
      </Card>

      <div className="flex flex-col gap-4 min-w-0">
        <Card>
          <CardHead title="Hazard scale" />
          {!legend ? (
            <Skeleton className="h-[180px]" />
          ) : (
            <>
              <p className="text-[13px] text-muted mb-3">
                {legend.label} in {legend.units}
              </p>
              <ul className="flex flex-col gap-1.5">
                {legend.stops.map((s) => (
                  <li key={s.from} className="flex items-center gap-2.5">
                    <i
                      aria-hidden
                      className="w-[22px] h-[12px] rounded-[3px] shrink-0 border border-line-2"
                      style={{ background: s.color }}
                    />
                    <span className="text-[12.5px] tabular-nums">
                      {s.to === null
                        ? `${s.from} and above`
                        : `${s.from} to ${s.to}`}
                    </span>
                  </li>
                ))}
                <li className="flex items-center gap-2.5">
                  <i
                    aria-hidden
                    className="w-[22px] h-[12px] rounded-[3px] shrink-0 border border-line-2"
                  />
                  <span className="text-[12.5px] text-muted">
                    below {legend.stops[0].from}, or not modelled
                  </span>
                </li>
              </ul>
              <p className="text-[12px] text-muted mt-4 leading-relaxed">
                {legend.index_label}. Coverage: {legend.coverage}. Source:{" "}
                {legend.source}. Tiles above zoom {legend.max_zoom} are the top
                pyramid level stretched, not extra detail.
              </p>
            </>
          )}
        </Card>

        <Card>
          <CardHead title="Assets" />
          <ul className="flex flex-col gap-1.5">
            {(["severe", "high", "moderate", "low"] as const)
              .filter((b) => (data.headline.bands[b] ?? 0) > 0)
              .map((b) => (
                <li key={b} className="flex items-center gap-2.5">
                  <i
                    aria-hidden
                    className="w-[11px] h-[11px] rounded-full shrink-0"
                    style={{ background: BAND_COLOR[b] }}
                  />
                  <span className="text-[13px]">{BAND_LABEL[b]}</span>
                  <span className="ml-auto text-[13px] font-semibold tabular-nums">
                    {data.headline.bands[b]}
                  </span>
                </li>
              ))}
          </ul>
          <p className="text-[12px] text-muted mt-4 leading-relaxed">
            Circle size is expected annual loss. Select a circle to open the
            asset.
          </p>
        </Card>
      </div>
    </div>
  );
}

function escapeHtml(s: string): string {
  return s.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ] as string,
  );
}
