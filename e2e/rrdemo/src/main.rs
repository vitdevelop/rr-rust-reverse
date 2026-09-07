#[derive(Debug)]
struct Point {
    x: i64,
    y: i64,
    label: String,
}

fn mutate(p: &mut Point, v: &mut Vec<i64>, round: i64) {
    p.x += round;
    p.y -= round * 2;
    p.label = format!("round-{round}");
    v.push(round * round);
}

fn main() {
    let mut p = Point {
        x: 0,
        y: 0,
        label: String::from("start"),
    };
    let mut v: Vec<i64> = Vec::new();

    for round in 1..=10 {
        mutate(&mut p, &mut v, round);
        println!("round {round}: p={:?} v={:?}", p, v);
    }

    let sum: i64 = v.iter().sum();
    println!("final: p={:?} sum={sum}", p);
}
