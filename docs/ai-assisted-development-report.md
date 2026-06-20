# Raport: folosirea toolurilor de AI in dezvoltarea software

Acest proiect a fost dezvoltat folosind extensiv tooluri de agentic AI. AI-ul
nu a fost folosit doar pentru autocomplete sau generare izolata de cod, ci ca
parte din procesul de inginerie: planificare pe taskuri, audit de cod,
implementare incrementala, verificare, documentare si iteratii pe baza
feedbackului.

## Tooluri folosite

Principalele tooluri de AI folosite in timpul dezvoltarii au fost:

- Codex cu GPT-5.5;
- Devin AI;
- Antigravity cu Claude Sonnet 4.6;
- Gemini CLI;
- GitHub Copilot.

Am folosit atat interfete de tip app, cat si TUI/CLI. In functie de task,
agentii primeau acces pe branchuri specifice si lucrau pe schimbari izolate.
Acest lucru a permis separarea mai clara a modificarilor, review mai usor si
reducerea riscului ca un agent sa schimbe parti nelegate de task.

## Modul de lucru

Taskurile au fost scrise explicit, de obicei in format Markdown, pentru a fi
usor de inteles de catre modelele AI. Un task tipic includea:

- contextul functional al schimbarii;
- scopul concret al taskului;
- fisiere sau module relevante;
- pasi incrementali de implementare;
- criterii de acceptare;
- comenzi de testare sau verificare;
- restrictii, de exemplu sa nu se modifice teste, sa nu se rescrie fisiere
  nelegate sau sa nu se expuna secrete.

Pentru taskurile mai mari, am impartit cerintele in pasi mici. De exemplu, un
agent primea mai intai cerinta de a intelege o zona din codebase, apoi de a
face o schimbare limitata, apoi de a rula testele relevante si de a explica
rezultatul. Acest proces incremental a facut mai usor de detectat cand un model
facea o presupunere gresita.

## Dezvoltare pe branchuri si taskuri din Linear

O parte din lucru a fost organizata in jurul taskurilor definite in Linear.
Pentru fiecare task, modelul primea o descriere clara si era directionat catre
branchul potrivit. Agentul putea sa citeasca repository-ul, sa propuna sau sa
aplice schimbari, sa ruleze teste si sa faca un rezumat tehnic al rezultatului.

Branchurile au fost folosite ca limita practica de lucru:

- fiecare schimbare importanta era izolata intr-un branch;
- commiturile erau grupate dupa scop;
- rezultatele agentilor puteau fi verificate inainte de merge;
- CI-ul si staging-ul validau ca modificarile functioneaza in contextul real al
  proiectului.

Aceasta abordare a fost importanta deoarece agentii pot produce schimbari bune,
dar trebuie totusi verificati prin review, teste si CI.

## Prompting si context scris in Markdown

Am folosit prompturi incrementale, clare si orientate pe rezultat. In loc sa
cerem "implementeaza feature-ul X" fara context, am descris ce trebuie facut,
care sunt constrangerile si cum se poate verifica rezultatul.

Markdown-ul a fost util pentru ca permite structurarea taskurilor in:

- obiectiv;
- context;
- pasi;
- criterii de acceptare;
- observatii;
- comenzi de verificare.

Am folosit agenti si pentru audituri de cod si pentru scrierea contextului in
fisiere Markdown. De exemplu, dupa ce un agent analiza o zona din proiect,
rezultatul putea fi pastrat ca documentatie sau backlog. Apoi, prin prompturi
incrementale, puteam continua de la acel context in loc sa reluam analiza de la
zero.

## Rolul agentilor in proiect

Agentii au fost folositi pentru mai multe tipuri de activitati:

- explorarea codebase-ului si identificarea modulelor relevante;
- implementarea de functionalitati;
- fixarea bugurilor aparute in CI sau staging;
- scrierea si actualizarea documentatiei;
- audit tehnic si identificarea riscurilor;
- explicarea arhitecturii pentru prezentare;
- verificarea comportamentului cu teste locale si GitHub Actions.

Un exemplu concret este fluxul de depanare pentru staging: agentul a folosit
`gh` pentru a inspecta logurile reale din GitHub Actions, a identificat ca
problema nu era doar in `rsync`, ci si in curatarea fisierelor generate cu
ownership de container, apoi a reparat scripturile si a verificat din nou
workflow-ul.

## Ce inseamna agentic AI in acest proces

In acest proiect, agentic AI a insemnat ca modelele nu au fost folosite doar
pentru sugestii pasive, ci pentru executarea unor bucle de lucru:

```text
cerinta -> citire context -> plan -> modificare -> testare -> explicare -> iteratie
```

Totusi, AI-ul nu a fost tratat ca sursa finala de adevar. Am pastrat un proces
de inginerie in jurul lui:

- taskuri clare;
- acces limitat pe branchuri;
- modificari incrementale;
- teste locale;
- CI;
- staging;
- review uman;
- documentatie pentru deciziile importante.

Aceasta combinatie este importanta. Agentii pot accelera dezvoltarea, dar
calitatea vine din limite, verificari si feedback.

## Beneficii observate

Folosirea toolurilor agentice a ajutat in special la:

- intelegerea mai rapida a unui codebase mare;
- transformarea unor cerinte ambigue in pasi concreti;
- generarea rapida de patchuri initiale;
- gasirea cauzei unor erori din CI prin loguri reale;
- scrierea de documentatie tehnica;
- mentinerea contextului intre iteratii prin fisiere Markdown;
- explorarea mai multor abordari fara a bloca dezvoltarea principala.

## Limitari si control

Modelele pot gresi. Din acest motiv, procesul a inclus masuri de control:

- nu se accepta schimbari fara verificare;
- agentii nu trebuie sa modifice fisiere nelegate de task;
- se folosesc teste relevante pentru fiecare schimbare;
- se verifica statusul Git inainte si dupa modificari;
- nu se expun secrete sau chei API;
- staging-ul si CI-ul sunt folosite ca validare finala.

Aceste limite au facut posibil sa folosim AI-ul ca un colaborator tehnic, nu ca
un mecanism nesupravegheat de generare de cod.

## Concluzie

Sandcastle a fost dezvoltat folosind agentic AI intr-un mod controlat si
ingineresc. Toolurile precum Codex, Devin AI, Antigravity, Gemini CLI si GitHub
Copilot au fost integrate in procesul de lucru prin taskuri clare, branchuri
dedicate, prompturi incrementale, audituri, documentatie si verificari automate.

Rezultatul nu este doar un proiect care foloseste AI in functionalitatea sa, ci
si un proiect construit printr-un proces care demonstreaza cum se poate lucra
responsabil cu agenti AI in dezvoltarea software.
