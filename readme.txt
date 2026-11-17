人才服务一网通办事项编排与补偿平台
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q one_stop_cases tests
验收：python3 -m one_stop_cases --database /tmp/one_stop_cases.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/one_stop_cases-output.json
单测：python3 -m unittest tests.test_domain -v

时效归因（按环节 / 事项版本 / 受理批次）：
汇总：python3 -m one_stop_cases timeliness --fixture fixtures/timeliness.json --output /tmp/timeliness-summary.json
个案：python3 -m one_stop_cases timeliness --fixture fixtures/timeliness.json --case case-minute
库内：python3 -m one_stop_cases --database /tmp/one_stop_cases.sqlite3 report --timeliness --as-of 2026-11-20T16:00:00+00:00
说明：归因复用 intake/service_clock 的工作时段与节假日口径；by_step 为各环节全部工作时间，
      by_category / chargeable_working_seconds 再区分可计费与顺延时间；
      暂停与材料补正另列并顺延承诺办结时间，失败后的补偿与重新办理计入可计费时间；
      缺少关键时间记录的事项标记“证据不足”（evidence_insufficient），不判定按时或逾期；
      排障瓶颈（bottlenecks）只使用证据充分的事项。
