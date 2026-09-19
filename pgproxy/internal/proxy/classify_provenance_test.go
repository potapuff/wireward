package proxy

import "testing"

// Відкрите питання з docs/07-roadmap.md, закрите 04.09.2026.
//
// У прогоні `make check-shape` два запити пройшли фільтр WORK_ONLY і виглядали
// як дірка в класифікаторі. Перш ніж правити класифікатор за слідами, які міг
// лишити він же старішої версії, перевірено походження — і воно пояснило
// обидва випадки, по-різному.
//
// Тести нижче лишаються сторожами: обидва висновки легко зламати назад.
func TestПідозріліЗапитиЗЖурналу(t *testing.T) {
	// 1. Читання переліку розширень драйвером. У журналі воно позначене як
	// робота студента — але записане 02–03.09, тобто ДО ADR-018 (коміт
	// 38cedbf, 03.09 20:59), який розширив розпізнавання каталогу до
	// будь-якого джерела з префіксом pg_. Поточний класифікатор його ловить,
	// і ось доказ.
	const розширення = `select E.oid   as extension_id,
       D.objid as member_id
from pg_extension E
     join pg_depend D on E.oid = D.refobjid and
                         D.refclassid = 'pg_extension'::regclass::oid
where D.deptype = 'e'
order by extension_id`
	if !IsCatalog(розширення) {
		t.Error("запит до pg_extension мусить бути каталогом: якщо ні — " +
			"клієнтський шум підмішується в покриття лабораторних і в EQ")
	}

	// 2. Курсор. Тут висновок протилежний, і саме він важливіший.
	//
	// Рядки, що виглядали клієнтськими, виявилися трафіком НАШОГО ж тесту
	// протоколу (студент «Проксі Тест», послідовність BEGIN / DECLARE /
	// FETCH×8 / CLOSE / COMMIT). Справжнього клієнта серед них не було.
	//
	// І окремо: `DECLARE ... CURSOR`. Тут висновок 04.09 був «робота
	// студента, бо курсори — тема курсу», і питання пішло викладачеві.
	// Відповідь 06.09.2026: у жодній лабораторній курсорів немає, отже
	// єдине джерело DECLARE у журналі — посторінкове гортання DataGrip і
	// DBeaver. Тому `declare` додано до reServiceWord: за ADR-018 хибне
	// спрацювання ховає рядок під галочкою, а не втрачає його.
	//
	// Якщо курсори колись з'являться в програмі — знімати треба саме
	// `declare` з reServiceWord, і цей тест має впасти першим.
	const курсор = `DECLARE "paged" CURSOR FOR select id, name from products order by id`
	if !IsService(курсор) {
		t.Error("DECLARE ... CURSOR — пагінація GUI-клієнта; курсорів у " +
			"лабораторних немає, тож рядок має ховатися як службовий")
	}
	// FETCH лишається роботою студента: сам по собі він у журналі не
	// з'являється без DECLARE, а ховати ще й його — розширювати евристику
	// далі, ніж підтвердив викладач.
	if IsCatalog(`FETCH FORWARD 7 FROM "paged"`) || IsService(`FETCH FORWARD 7 FROM "paged"`) {
		t.Error("FETCH окремо не ховаємо: евристика не ширша за підтверджене")
	}
}

// Межа нового слова `declare` у reServiceWord: воно має ловити оператор
// DECLARE, а не будь-яку згадку слова. Найнебезпечніший сусід — PL/pgSQL,
// де DECLARE стоїть усередині тіла функції: такий CREATE FUNCTION лишається
// роботою студента цілком.
func TestDeclareНеЛовитьЗайвого(t *testing.T) {
	work := []string{
		`CREATE FUNCTION f() RETURNS int AS $$ DECLARE x int; BEGIN RETURN 1; END $$ LANGUAGE plpgsql`,
		`DO $$ DECLARE x int; BEGIN x := 1; END $$`,
		`SELECT * FROM declarations`,
		`SELECT 'DECLARE cursor' AS t`,
	}
	for _, sql := range work {
		if IsService(sql) {
			t.Errorf("IsService(%q) = true, а це робота студента", sql)
		}
	}
	for _, sql := range []string{
		`DECLARE "paged" CURSOR FOR SELECT 1`,
		"  declare c cursor with hold for select id from products",
		"/* DataGrip */ DECLARE c1 CURSOR FOR SELECT * FROM t",
	} {
		if !IsService(sql) {
			t.Errorf("IsService(%q) = false, а це DECLARE CURSOR клієнта", sql)
		}
	}
}
