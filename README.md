# The Lumon Innie Task Scheduler

Welcome to Lumon Industries.

Every Innie arrives at their desk with a list of work orders for the day. They load a number,
add to it, multiply it, and eventually **WAFFLE** — the moment their work is finalised and made
visible to everyone else on the floor. Whatever an Innie has WAFFLEd is their work product, and
it is the only thing their colleagues are ever allowed to see.

This project runs those workdays.

Give it a **work schedule** — a list of Innies and the orders each of them has been assigned —
and it carries out the whole day, then reports what every Innie finished with.

## What makes a workday interesting

Innies do not work in isolation. An order can say *"add whatever HELLY and MARK ended up with."*
When it does, that Innie simply waits at their desk until those colleagues have WAFFLEd, then
picks up the day where they left off. Nobody ever sees half-finished work — an Innie's day is
either finished and published, or it isn't there at all.

The floor is busy, too. Every Innie works **at the same time** as every other Innie, not one
after another. Despite that, the same schedule always produces exactly the same results. The
severed mind works the same way every time.

Some days are stranger than others:

- **Wellness checks.** An order can be interrupted by a wellness event, which voids that Innie's
  work and resets them to nothing.
- **Conditional work.** An Innie can be told to take on extra work only if some condition about
  their colleagues holds — otherwise they skip it and move on.
- **Shifts.** A block of orders can be repeated a set number of times before the day continues.
- **Waiting on the room.** A condition can depend on *any* colleague meeting it, or on *all* of
  them meeting it. The Innie stops waiting the instant the answer is settled, rather than sitting
  idle for news that can no longer change anything.

## Nobody stays late

Occasionally a schedule asks for the impossible: HELLY is waiting on MARK, and MARK is waiting
right back on HELLY. Neither of them can ever finish.

Rather than let those Innies sit at their desks forever, the scheduler notices the circle,
declares the situation unworkable, and has everyone caught in it WAFFLE a value of **-1**. The
day ends. Every Innie clocks out.

---

*This is a home exercise. The full brief lives in `Hometask Backend/`.*
