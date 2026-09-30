# Agente Controlador de Mercado

Subagente de inteligencia comercial para el mercado cubano dentro de un sistema multiagente:

| Agente | Responsabilidad |
|---|---|
| **Controlador de Mercado** (este repositorio) | Mide el entorno externo: precios, oferta, vendedores, tendencias y señales. |
| Agente Interno | Inventario, ventas, costes y situación interna del negocio. |
| Agente Central | Cruza ambas fuentes y propone decisiones al propietario. |

Este agente **no decide** (no compra, no fija precios, no evalúa rentabilidad del negocio). Transforma observaciones públicas del mercado en un contrato estructurado, trazable y con nivel de confianza explícito.

## Arquitectura

```
Fuentes autorizadas ──► SourceRegistry ──► MarketAnalyzer (determinista) ──► contrato JSON
 (SourceAdapter)        registra fallos     pasos 2–13 del análisis              │
                                                                                 ▼
                         MarketControllerAgent (Claude API, tool use) ──► conclusiones etiquetadas
                         especifica el producto, revisa ambigüedades,     + fusión con el contrato
                         redacta conclusiones externas
```

- **Las cifras las calcula siempre el motor determinista** (`MarketAnalyzer`). El modelo no reescribe números.
- El modelo solo puede **reducir** la confianza calculada, nunca elevarla (se aplica en código).
- Las conclusiones con lenguaje de decisión ("debería comprar…") se marcan con `boundary_warning`.
- El contenido scrapeado se trata como datos: los textos con apariencia de instrucciones se registran como problema de calidad (`POSIBLE_INYECCION_DE_INSTRUCCIONES`).

| Módulo | Función |
|---|---|
| `models.py` | `Observation` (conserva `raw`), `TargetProduct`, `ExchangeRate`, `ReferenceCost`. |
| `normalization.py` | Monedas (`$` se considera ambiguo), unidades (ml/L, g/kg/lb, unidades), paquetes (`6 x 330 ml`), disponibilidad y URLs. |
| `matching.py` | Coincidencia EXACT / HIGH / MEDIUM / LOW / NO_MATCH con razones auditables. |
| `dedup.py` | Duplicados por señales fuertes; las coincidencias débiles se marcan y no se eliminan. |
| `stats.py` | Estadísticas robustas, outliers (MAD con suelo del 5 %, IQR o razón a la mediana) y concentración (HHI de anuncios). |
| `temporal.py` | Clasificación CAMBIO_PUNTUAL / MOVIMIENTO_RECIENTE / TENDENCIA / APARICION / DESAPARICION. |
| `signals.py` | Patrones de mercado (PRICE_INCREASE, POSSIBLE_SHORTAGE, HIGH_PRICE_DISPERSION…) con evidencia numérica. |
| `analyzer.py` | Orquesta el análisis y produce el contrato. |
| `sources.py` | `SourceAdapter`, `JsonFileSource`, `SourceRegistry`, `ExchangeRateProvider`. |
| `agent.py` | Bucle de tool use sobre la Claude API y fusión del resultado. |
| `prompts/system_prompt_es.md` | System prompt del agente. |

## Instalación

```bash
pip install -e ".[dev]"
python -m pytest
```

## Uso

### Análisis determinista (sin LLM)

```bash
python examples/generar_datos_sinteticos.py   # genera DATOS SINTÉTICOS de demostración
controlador-mercado analizar \
  --producto @examples/producto_aceite.json \
  --fuente examples/datos_sinteticos \
  --inicio 2026-09-23T12:00:00Z --fin 2026-09-30T12:00:00Z --ahora 2026-09-30T12:00:00Z \
  --salida informe.json
```

Opciones: `--granularidad day|week|month`, `--convertir-a CUP --tipos-cambio tasas.json` (solo con tasas autorizadas y fechadas), `--coste-referencia 800:CUP` (simulación de margen potencial etiquetada como estimación), `--sin-traza`.

### Agente completo (Claude API)

Requiere credenciales de la API de Anthropic (`ANTHROPIC_API_KEY` o `ant auth login`).

```bash
controlador-mercado agente "Analiza el aceite de girasol Ole de 1 L en La Habana" \
  --fuente datos/ --salida informe.json
```

Por defecto usa `claude-sonnet-5-5` (Claude Sonnet 5.5) con pensamiento adaptativo, esfuerzo `high` (variables `CONTROLADOR_MODEL` y `CONTROLADOR_EFFORT`), salida estructurada con JSON Schema y fallback del lado del servidor ante rechazos (`fallbacks: "default"`, solo en la Claude API; en Sonnet 5.5 reintenta los rechazos de categoría `cyber` y `frontier_llm`).

```python
from controlador_mercado import JsonFileSource, SourceRegistry
from controlador_mercado.agent import MarketControllerAgent

agent = MarketControllerAgent(SourceRegistry([JsonFileSource("datos/revolico.jsonl", "revolico")]))
report = agent.run("Precio y oferta de Coca-Cola lata 330 ml en Santiago de Cuba")
print(report.to_dict(include_traces=False))
```

## Fuentes de datos

El agente **no incluye scrapers**: consume observaciones de fuentes autorizadas. Para conectar una fuente nueva, implementa `SourceAdapter.fetch()`; las excepciones se registran como fuente fallida y reducen la confianza, nunca se sustituyen por datos inventados.

Formato de una observación (JSON / JSONL):

```json
{
  "source_id": "revolico", "source_name": "Revolico", "listing_id": "123", "url": "https://…",
  "title": "Aceite de girasol Ole 1 L", "brand": "Ole", "seller": "Tienda X",
  "province": "La Habana", "municipality": "Plaza",
  "price": 1150, "currency": "CUP", "quantity": 1, "unit": "L", "pack_count": 1,
  "availability": "disponible", "condition": "nuevo", "image_id": "abc",
  "captured_at": "2026-09-29T10:00:00Z", "published_at": "2026-09-27T08:00:00Z"
}
```

Solo son obligatorios `title` y `captured_at` (sin `captured_at` la observación se excluye). Si faltan `quantity`/`unit`, se intentan extraer del título.

## Contrato de salida

Cada análisis devuelve `product`, `analysis_period` (`analysis_start`, `analysis_end`, `analysis_generated_at`, `observation_count`), `market_summary` (afirmaciones etiquetadas HECHO_OBSERVADO / ESTADISTICA_CALCULADA / ESTIMACION / INFERENCIA / DATO_NO_DISPONIBLE), `price_statistics` (por moneda: precio por anuncio y por unidad estándar, por provincia y por fuente), `supply_statistics`, `trends`, `external_indicators`, `market_signals`, `sources`, `data_quality`, `confidence` + `confidence_factors`, `limitations` y `observations` (traza por observación con `raw_value` / `normalized_value`, coincidencia, duplicados y outliers). Una métrica no calculable es `null`.

### Reglas principales

- **Monedas:** nunca se mezclan. Solo se convierte con un tipo de cambio autorizado y fechado; el resultado se etiqueta como ESTIMACION y se conservan los precios originales.
- **Presentación:** las estadísticas por anuncio usan solo la misma presentación que el objetivo. Las demás presentaciones solo entran en el precio por unidad estándar (CUP/L, CUP/kg, CUP/unidad).
- **Coincidencia:** LOW y NO_MATCH quedan fuera de las estadísticas. MEDIUM se incluye, se cuenta aparte y reduce la confianza.
- **Outliers:** se marcan y se excluyen de las estadísticas principales, pero se conservan en la traza. El precio típico es la mediana.
- **Tendencias:** hacen falta ≥ 3 periodos, una variación ≥ umbral, ≥ 2/3 de los cambios en la misma dirección y un movimiento ya visible antes del último periodo. Dos puntos son como mucho un CAMBIO_PUNTUAL. Las tendencias de oferta usan solo periodos completos y fuentes presentes en todos ellos, para no confundir cambios de cobertura del scraping con cambios de oferta.
- **Demanda:** no hay datos de ventas; el número de anuncios y la disponibilidad se presentan como proxies.
- **Confianza:** puntuación explícita (tamaño de muestra, fuentes, calidad de coincidencia, actualidad, outliers, fallos de fuente, diferencias geográficas o entre fuentes). Es LOW forzada con n < 5 o con datos de más de 30 días.

Los umbrales son configurables en `AnalyzerConfig`.

## Datos de ejemplo

`examples/datos_sinteticos/` contiene **datos sintéticos** generados por `examples/generar_datos_sinteticos.py` (fuentes `sintetico_*`, URLs `ejemplo.invalid`). No representan el mercado real y solo sirven para probar el pipeline.
