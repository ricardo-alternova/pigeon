# Rules

| ID | Patrón | Acción | Alcance | Fuente |
| --- | --- | --- | --- | --- |
| R1 | billing@example.com | archive | todas | own |
| R2 | example.org | trash | todas | own |
| R3 | *.example.net | archive | todas | own |
| R4 | subject: Weekly digest * | trash | todas | own |
| R5 | promo@example.com | trash, except mail about an order | todas | promo.md |
| R6 | news@example.com | baja HTTPS + trash | todas | own |
| R7 | reports@example.com | move Archive/Reports | todas | own |
| R8 | alerts@example.com | trash | work | own |
| R9 | newsletter@example.org | revisar a mano | todas | own |
